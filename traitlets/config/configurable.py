"""A base class for objects that are configurable."""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
from __future__ import annotations

import contextlib
import logging
import sys
import typing as t
from contextvars import ContextVar
from copy import deepcopy
from textwrap import dedent

from traitlets.traitlets import (
    Any,
    Container,
    Dict,
    HasTraits,
    Instance,
    TraitType,
    default,
    observe,
    observe_compat,
    validate,
)
from traitlets.utils import warnings
from traitlets.utils.bunch import Bunch
from traitlets.utils.text import indent, wrap_paragraphs

from .loader import (
    SOURCE_DEFAULT,
    Config,
    DeferredConfig,
    LazyConfigValue,
    SourceRecord,
    _is_section_key,
)

if t.TYPE_CHECKING:
    from typing_extensions import Self

# -----------------------------------------------------------------------------
# Helper classes for Configurables
# -----------------------------------------------------------------------------

if t.TYPE_CHECKING:
    LoggerType = logging.Logger | logging.LoggerAdapter[t.Any]
else:
    LoggerType = t.Any


class ConfigurableError(Exception):
    pass


class MultipleInstanceError(ConfigurableError):
    pass


# -----------------------------------------------------------------------------
# Configurable implementation
# -----------------------------------------------------------------------------


class Configurable(HasTraits):
    config = Instance(Config, (), {})
    parent = Instance("traitlets.config.configurable.Configurable", allow_none=True)

    def __init__(self, **kwargs: t.Any) -> None:
        """Create a configurable given a config config.

        Parameters
        ----------
        config : Config
            If this is empty, default values are used. If config is a
            :class:`Config` instance, it will be used to configure the
            instance.
        parent : Configurable instance, optional
            The parent Configurable instance of this object.

        Notes
        -----
        Subclasses of Configurable must call the :meth:`__init__` method of
        :class:`Configurable` *before* doing anything else and using
        :func:`super`::

            class MyConfigurable(Configurable):
                def __init__(self, config=None):
                    super(MyConfigurable, self).__init__(config=config)
                    # Then any other code you need to finish initialization.

        This ensures that instances will be configured properly.
        """
        parent = kwargs.pop("parent", None)
        if parent is not None:
            # config is implied from parent
            if kwargs.get("config") is None:
                kwargs["config"] = parent.config
            self.parent = parent

        config = kwargs.pop("config", None)

        # provenance of the effective value of each configurable trait:
        # {trait_name: {"records": [...], "elements": aligned sources}}
        self._config_trait_sources: dict[str, dict[str, t.Any]] = {}

        # load kwarg traits, other than config
        super().__init__(**kwargs)

        # record traits set by config
        config_override_names = set()

        def notice_config_override(change: Bunch) -> None:
            """Record traits set by both config and kwargs.

            They will need to be overridden again after loading config.
            """
            if change.name in kwargs:
                config_override_names.add(change.name)

        self.observe(notice_config_override)

        # load config
        if config is not None:
            # We used to deepcopy, but for now we are trying to just save
            # by reference.  This *could* have side effects as all components
            # will share config. In fact, I did find such a side effect in
            # _config_changed below. If a config attribute value was a mutable type
            # all instances of a component were getting the same copy, effectively
            # making that a class attribute.
            # self.config = deepcopy(config)
            self.config = config
        else:
            # allow _config_default to return something
            self._load_config(self.config)
        self.unobserve(notice_config_override)

        for name in config_override_names:
            setattr(self, name, kwargs[name])
            # explicit constructor arguments take precedence over config
            self._config_trait_sources[name] = {
                "records": [SourceRecord("constructor", "set")],
                "elements": None,
            }

        # traits passed as plain constructor kwargs are explicit overrides
        # too; mark them unless config loading already explained the value
        trait_names = set(self.traits(config=True))
        for name in kwargs:
            if name in trait_names and name not in self._config_trait_sources:
                self._config_trait_sources[name] = {
                    "records": [SourceRecord("constructor", "set")],
                    "elements": None,
                }

    # -------------------------------------------------------------------------
    # Static trait notifications
    # -------------------------------------------------------------------------

    @classmethod
    def section_names(cls) -> list[str]:
        """return section names as a list"""
        return [
            c.__name__
            for c in reversed(cls.__mro__)
            if issubclass(c, Configurable) and issubclass(cls, c)
        ]

    def _find_my_config(self, cfg: Config) -> t.Any:
        """extract my config from a global Config object

        will construct a Config object of only the config values that apply to me
        based on my mro(), as well as those of my parent(s) if they exist.

        If I am Bar and my parent is Foo, and their parent is Tim,
        this will return merge following config sections, in this order::

            [Bar, Foo.Bar, Tim.Foo.Bar]

        With the last item being the highest priority.
        """
        cfgs = [cfg]
        if self.parent:
            cfgs.append(self.parent._find_my_config(cfg))
        my_config = Config()
        for c in cfgs:
            for sname in self.section_names():
                # Don't do a blind getattr as that would cause the config to
                # dynamically create the section with name Class.__name__.
                if c._has_section(sname):
                    my_config.merge(c[sname])
        return my_config

    def _load_config(
        self,
        cfg: Config,
        section_names: list[str] | None = None,
        traits: dict[str, TraitType[t.Any, t.Any]] | None = None,
    ) -> None:
        """load traits from a Config object"""

        my_config = self._find_my_config(cfg)
        if not my_config:
            # Nothing in the config applies to this instance; avoid the cost of
            # computing traits() and entering hold_trait_notifications() for the
            # many leaf Configurables that carry no matching config.
            return

        if traits is None:
            traits = self.traits(config=True)

        # hold trait notifications until after all config has been loaded
        with self.hold_trait_notifications():
            for name, config_value in my_config.items():
                if name in traits:
                    if isinstance(config_value, LazyConfigValue):
                        # ConfigValue is a wrapper for using append / update on containers
                        # without having to copy the initial value
                        initial = getattr(self, name)
                        config_value, element_sources = config_value.resolve(initial)
                    elif isinstance(config_value, DeferredConfig):
                        # DeferredConfig tends to come from CLI/environment variables
                        config_value = config_value.get_value(traits[name])
                        element_sources = None
                    else:
                        element_sources = None
                    # We have to do a deepcopy here if we don't deepcopy the entire
                    # config object. If we don't, a mutable config_value will be
                    # shared by all instances, effectively making it a class attribute.
                    setattr(self, name, deepcopy(config_value))
                    self._record_trait_source(
                        name, config_value, my_config, element_sources
                    )
                elif not _is_section_key(name) and not isinstance(config_value, Config):
                    from difflib import get_close_matches

                    if isinstance(self, LoggingConfigurable):
                        assert self.log is not None
                        warn = self.log.warning
                    else:

                        def warn(msg: t.Any) -> None:
                            return warnings.warn(msg, UserWarning, stacklevel=9)

                    matches = get_close_matches(name, traits)
                    msg = f"Config option `{name}` not recognized by `{self.__class__.__name__}`."

                    if len(matches) == 1:
                        msg += f"  Did you mean `{matches[0]}`?"
                    elif len(matches) >= 1:
                        msg += "  Did you mean one of: `{matches}`?".format(
                            matches=", ".join(sorted(matches))
                        )
                    warn(msg)

    def _record_trait_source(
        self,
        name: str,
        value: t.Any,
        my_config: Config,
        element_sources: t.Any,
    ) -> None:
        """Store resolved provenance for one trait after loading config.

        Only readable source labels and operation names are stored;
        trait values are never duplicated here.
        """
        trace = my_config.get_trace(name)
        if trace is None:
            records: list[SourceRecord] = []
        else:
            records = list(trace.records)

        if element_sources is None and trace is not None:
            element_sources = trace.element_sources

        if not records:
            records = [SourceRecord(SOURCE_DEFAULT, "default")]

        self._config_trait_sources[name] = {
            "records": records,
            "elements": element_sources,
        }

    def config_source_records(self, trait_name: str) -> list[SourceRecord]:
        """Return the provenance chain for ``trait_name``, oldest first.

        Every entry is a :class:`~traitlets.config.loader.SourceRecord`
        holding a readable source label (e.g. a file path,
        ``command-line``, ``environment``, ``unknown``, ``default``)
        and the operation that set or changed the value
        (``set``, ``append``, ``prepend``, ``insert``, ``update``,
        ``default``).

        The last entry explains the final value. For traits whose final
        value is the class default the chain contains the single
        ``("default", "default")`` record.
        """
        info = self._config_trait_sources.get(trait_name)
        if info is not None:
            return list(info["records"])
        if trait_name in self.traits(config=True):
            return [SourceRecord(SOURCE_DEFAULT, "default")]
        raise AttributeError(
            f"{self.__class__.__name__!s} has no config trait {trait_name!r}"
        )

    def config_source(self, trait_name: str) -> str:
        """Return the readable source label of the final trait value.

        Examples: a config file path, ``command-line``, ``environment``,
        ``unknown`` for values supplied directly in user code, and
        ``default`` when the class default is in effect.
        """
        return self.config_source_records(trait_name)[-1].source

    def config_element_sources(self, trait_name: str) -> t.Any:
        """Return per-element provenance for a container trait.

        * list traits return a list of :class:`SourceRecord`
        * dict traits return a ``{key: SourceRecord}`` mapping
        * set traits return a ``{element: SourceRecord}`` mapping
        * scalars return a single :class:`SourceRecord`

        Raises ``TypeError`` if the trait is not a supported container.
        """
        self.config_source_records(trait_name)  # existence check
        info = self._config_trait_sources.get(trait_name, {})
        elements = info.get("elements")
        if elements is not None:
            return elements
        record = self.config_source_records(trait_name)[-1]
        value = getattr(self, trait_name)
        if isinstance(value, list):
            return [record] * len(value)
        if isinstance(value, (dict, set)):
            return {key: record for key in value}
        return record

    def explain_config_value(
        self,
        trait_name: str,
        include_value: bool = False,
    ) -> str:
        """Return a human-readable explanation of where a trait value came from.

        Lists the full override chain (oldest to newest) and the winning
        source. For container traits, per-element sources are included.

        Config values are never shown unless ``include_value=True`` is
        explicitly passed. This method only inspects provenance metadata
        recorded while loading config; it never re-executes Python config
        files.
        """
        records = self.config_source_records(trait_name)
        lines = [f"{self.__class__.__name__}.{trait_name}:"]
        lines.append("  source chain (oldest first):")
        for record in records:
            if record.operation in {"set", "default"}:
                lines.append(f"    - {record.source}")
            else:
                lines.append(f"    - {record.source} ({record.operation})")
        lines.append(f"  final source: {records[-1].source}")

        elements = None
        info = self._config_trait_sources.get(trait_name)
        if info is not None:
            elements = info.get("elements")
        value = getattr(self, trait_name)
        if isinstance(value, list) and elements is not None:
            lines.append("  items:")
            for item, record in zip(value, elements):
                shown = repr(item) if include_value else "<value>"
                if record.operation == "set":
                    lines.append(f"    - {shown}: {record.source}")
                else:
                    lines.append(
                        f"    - {shown}: {record.source} ({record.operation})"
                    )
        elif isinstance(value, dict) and elements is not None:
            lines.append("  entries:")
            for key, record in elements.items():
                key_shown = repr(key) if include_value else "<key>"
                if record.operation == "set":
                    lines.append(f"    - {key_shown}: {record.source}")
                else:
                    lines.append(
                        f"    - {key_shown}: {record.source} ({record.operation})"
                    )

        if include_value:
            lines.append(f"  value: {value!r}")
        return "\n".join(lines)

    @observe("config")
    @observe_compat
    def _config_changed(self, change: Bunch) -> None:
        """Update all the class traits having ``config=True`` in metadata.

        For any class trait with a ``config`` metadata attribute that is
        ``True``, we update the trait with the value of the corresponding
        config entry.
        """
        # Get all traits with a config metadata entry that is True
        traits = self.traits(config=True)

        # We auto-load config section for this class as well as any parent
        # classes that are Configurable subclasses.  This starts with Configurable
        # and works down the mro loading the config for each section.
        section_names = self.section_names()
        self._load_config(change.new, traits=traits, section_names=section_names)

    def update_config(self, config: Config) -> None:
        """Update config and load the new values"""
        # traitlets prior to 4.2 created a copy of self.config in order to trigger change events.
        # Some projects (IPython < 5) relied upon one side effect of this,
        # that self.config prior to update_config was not modified in-place.
        # For backward-compatibility, we must ensure that self.config
        # is a new object and not modified in-place,
        # but config consumers should not rely on this behavior.
        self.config = deepcopy(self.config)
        # load config
        self._load_config(config)
        # merge it into self.config
        self.config.merge(config)
        # TODO: trigger change event if/when dict-update change events take place
        # DO NOT trigger full trait-change

    @classmethod
    def class_get_help(cls, inst: HasTraits | None = None) -> str:
        """Get the help string for this class in ReST format.

        If `inst` is given, its current trait values will be used in place of
        class defaults.
        """
        assert inst is None or isinstance(inst, cls)
        final_help = []
        base_classes = ", ".join(p.__name__ for p in cls.__bases__)
        final_help.append(f"{cls.__name__}({base_classes}) options")
        final_help.append(len(final_help[0]) * "-")
        for _, v in sorted(cls.class_traits(config=True).items()):
            help = cls.class_get_trait_help(v, inst)
            final_help.append(help)
        return "\n".join(final_help)

    @classmethod
    def class_get_trait_help(
        cls,
        trait: TraitType[t.Any, t.Any],
        inst: HasTraits | None = None,
        helptext: str | None = None,
    ) -> str:
        """Get the helptext string for a single trait.

        :param inst:
            If given, its current trait values will be used in place of
            the class default.
        :param helptext:
            If not given, uses the `help` attribute of the current trait.
        """
        assert inst is None or isinstance(inst, cls)
        lines = []
        header = f"--{cls.__name__}.{trait.name}"
        if isinstance(trait, (Container, Dict)):
            multiplicity = trait.metadata.get("multiplicity", "append")
            if isinstance(trait, Dict):
                sample_value = "<key-1>=<value-1>"
            else:
                sample_value = f"<{trait.__class__.__name__.lower()}-item-1>"
            if multiplicity == "append":
                header = f"{header}={sample_value}..."
            else:
                header = f"{header} {sample_value}..."
        else:
            header = f"{header}=<{trait.__class__.__name__}>"
        # header = "--%s.%s=<%s>" % (cls.__name__, trait.name, trait.__class__.__name__)
        lines.append(header)

        if helptext is None:
            helptext = trait.help
        if helptext != "":
            helptext = "\n\n".join(wrap_paragraphs(helptext, 76))
            lines.append(indent(helptext))

        if "Enum" in trait.__class__.__name__:
            # include Enum choices
            lines.append(indent(f"Choices: {trait.info()}"))

        if inst is not None:
            lines.append(indent(f"Current: {getattr(inst, trait.name or '')!r}"))
        else:
            try:
                dvr = trait.default_value_repr()
            except Exception:
                dvr = None  # ignore defaults we can't construct
            if dvr is not None:
                if len(dvr) > 64:
                    dvr = dvr[:61] + "..."
                lines.append(indent(f"Default: {dvr}"))

        return "\n".join(lines)

    @classmethod
    def class_print_help(cls, inst: HasTraits | None = None) -> None:
        """Get the help string for a single trait and print it."""
        print(cls.class_get_help(inst))  # noqa: T201

    @classmethod
    def _defining_class(
        cls, trait: TraitType[t.Any, t.Any], classes: t.Sequence[type[HasTraits]]
    ) -> type[Configurable]:
        """Get the class that defines a trait

        For reducing redundant help output in config files.
        Returns the current class if:
        - the trait is defined on this class, or
        - the class where it is defined would not be in the config file

        Parameters
        ----------
        trait : Trait
            The trait to look for
        classes : list
            The list of other classes to consider for redundancy.
            Will return `cls` even if it is not defined on `cls`
            if the defining class is not in `classes`.
        """
        defining_cls = cls
        assert trait.name is not None
        for parent in cls.mro():
            if (
                issubclass(parent, Configurable)
                and parent in classes
                and parent.class_own_traits(config=True).get(trait.name, None) is trait
            ):
                defining_cls = parent
        return defining_cls

    @classmethod
    def class_config_section(cls, classes: t.Sequence[type[HasTraits]] | None = None) -> str:
        """Get the config section for this class.

        Parameters
        ----------
        classes : list, optional
            The list of other classes in the config file.
            Used to reduce redundant information.
        """

        def c(s: str) -> str:
            """return a commented, wrapped block."""
            s = "\n\n".join(wrap_paragraphs(s, 78))

            return "## " + s.replace("\n", "\n#  ")

        # section header
        breaker = "#" + "-" * 78
        parent_classes = ", ".join(p.__name__ for p in cls.__bases__ if issubclass(p, Configurable))

        s = f"# {cls.__name__}({parent_classes}) configuration"
        lines = [breaker, s, breaker]
        # get the description trait
        desc = cls.class_traits().get("description")
        if desc:
            desc = desc.default_value
        if not desc:
            # no description from trait, use __doc__
            desc = getattr(cls, "__doc__", "")  # type:ignore[arg-type]
        if desc:
            lines.append(c(desc))  # type:ignore[arg-type]
            lines.append("")

        for name, trait in sorted(cls.class_traits(config=True).items()):
            default_repr = trait.default_value_repr()

            if classes:
                defining_class = cls._defining_class(trait, classes)
            else:
                defining_class = cls
            if defining_class is cls:
                # cls owns the trait, show full help
                if trait.help:
                    lines.append(c(trait.help))
                if "Enum" in type(trait).__name__:
                    # include Enum choices
                    lines.append(f"#  Choices: {trait.info()}")
                lines.append(f"#  Default: {default_repr}")
            else:
                # Trait appears multiple times and isn't defined here.
                # Truncate help to first line + "See also Original.trait"
                if trait.help:
                    lines.append(c(trait.help.split("\n", 1)[0]))
                lines.append(f"#  See also: {defining_class.__name__}.{name}")

            lines.append(f"# c.{cls.__name__}.{name} = {default_repr}")
            lines.append("")
        return "\n".join(lines)

    @classmethod
    def class_config_rst_doc(cls) -> str:
        """Generate rST documentation for this class' config options.

        Excludes traits defined on parent classes.
        """
        lines = []
        classname = cls.__name__
        for _, trait in sorted(cls.class_traits(config=True).items()):
            ttype = trait.__class__.__name__

            if not trait.name:
                continue
            termline = classname + "." + trait.name

            # Choices or type
            if "Enum" in ttype:
                # include Enum choices
                termline += " : " + trait.info_rst()  # type:ignore[attr-defined]
            else:
                termline += " : " + ttype
            lines.append(termline)

            # Default value
            try:
                dvr = trait.default_value_repr()
            except Exception:
                dvr = None  # ignore defaults we can't construct
            if dvr is not None:
                if len(dvr) > 64:
                    dvr = dvr[:61] + "..."
                # Double up backslashes, so they get to the rendered docs
                dvr = dvr.replace("\\n", "\\\\n")
                lines.append(indent(f"Default: ``{dvr}``"))
                lines.append("")

            help = trait.help or "No description"
            lines.append(indent(dedent(help)))

            # Blank line
            lines.append("")

        return "\n".join(lines)


class LoggingConfigurable(Configurable):
    """A parent class for Configurables that log.

    Subclasses have a log trait, and the default behavior
    is to get the logger from the currently running Application.
    """

    log = Any(help="Logger or LoggerAdapter instance", allow_none=False)

    @validate("log")
    def _validate_log(self, proposal: Bunch) -> LoggerType:
        if not isinstance(proposal.value, (logging.Logger, logging.LoggerAdapter)):
            # warn about unsupported type, but be lenient to allow for duck typing
            warnings.warn(
                f"{self.__class__.__name__}.log should be a Logger or LoggerAdapter,"
                f" got {proposal.value}.",
                UserWarning,
                stacklevel=2,
            )
        return t.cast(LoggerType, proposal.value)

    @default("log")
    def _log_default(self) -> LoggerType:
        if isinstance(self.parent, LoggingConfigurable):
            assert self.parent is not None
            return t.cast(logging.Logger, self.parent.log)
        from traitlets import log

        return log.get_logger()

    def _get_log_handler(self) -> logging.Handler | None:
        """Return the default Handler

        Returns None if none can be found

        .. deprecated:: 5.2

            This now returns the first log handler, which may or may not be
            the default one. Use ``self.log.handlers`` directly instead.
        """
        warnings.warn(
            "Configurable._get_log_handler has been deprecated since traitlets 5.2 - 2022,"
            " use the `.handlers` attribute of the logger directly.",
            DeprecationWarning,
            stacklevel=2,
        )
        if not self.log:
            return None
        logger: logging.Logger = (
            self.log if isinstance(self.log, logging.Logger) else self.log.logger
        )
        if not getattr(logger, "handlers", None):
            # no handlers attribute or empty handlers list
            return None
        return logger.handlers[0]


CT = t.TypeVar("CT", bound="SingletonConfigurable")


_active_scopes: ContextVar[tuple[SingletonScope, ...]] = ContextVar("singleton_scopes", default=())


def _threads_inherit_context() -> bool:
    """Whether a new :class:`threading.Thread` inherits the caller's context.

    Governed by :data:`sys.flags.thread_inherit_context`: false by default on the
    GIL build, true by default on the free-threaded build (e.g. ``3.14t``) and on
    any build launched with ``-X thread_inherit_context=1``. When true, an active
    :class:`SingletonScope` propagates into threads started while it is active.
    """
    return bool(getattr(sys.flags, "thread_inherit_context", 0))


class SingletonScope:
    """An isolated singleton registry, activated for a dynamic extent.

    While active (``with scope():``), :meth:`SingletonConfigurable.instance`
    on any subclass of ``base`` resolves within this registry — creating
    fresh instances on first use — in the current thread/async task.
    Re-enterable: the registry persists across activations.

    .. warning::

        The scope lives in a :class:`~contextvars.ContextVar`. On builds where
        :data:`sys.flags.thread_inherit_context` is enabled — the default on the
        free-threaded build (e.g. ``3.14t``) — a :class:`threading.Thread`
        started while the scope is active inherits a copy of the parent context
        and resolves ``.instance()`` within the scope instead of the
        process-global registry. Activating a scope under that flag emits a
        :class:`RuntimeWarning`. For per-thread isolation, activate a fresh scope
        inside each thread rather than relying on non-inheritance.

    Scope ``SingletonConfigurable`` itself for a complete blank slate covering
    every singleton in the process.

    .. versionadded:: 5.17
    """

    def __init__(self, base: type[SingletonConfigurable]) -> None:
        self.base = base
        self._instances: dict[type, SingletonConfigurable] = {}

    def covers(self, cls: type) -> bool:
        """Whether ``cls`` resolves within this scope.

        .. versionadded:: 5.17
        """
        return issubclass(cls, self.base)

    def get(self, cls: type[CT]) -> CT | None:
        """Registered instance for ``cls``, emulating class-attribute
        inheritance: walk ``cls.__mro__`` and return the first hit.

        .. versionadded:: 5.17
        """
        for klass in cls.__mro__:
            if klass in self._instances:
                return t.cast("CT", self._instances[klass])
        return None

    def add(self, inst: SingletonConfigurable) -> None:
        """Pre-seed the registry with an instance you built yourself, so that
        ``.instance()`` returns *that* object inside the scope.

        Writes through to the singleton parents of ``type(inst)``, the same way
        the classic global path does, so a subclass instance is also returned
        when resolving via an ancestor class. This is the only way to control
        *which* object in-scope ``.instance()`` calls receive, since they
        otherwise construct a fresh one.

        .. versionadded:: 5.17
        """
        for subclass in type(inst)._walk_mro():
            self._instances[subclass] = inst

    @contextlib.contextmanager
    def __call__(self) -> t.Generator[SingletonScope, None, None]:
        if _threads_inherit_context():
            warnings.warn(
                "A SingletonScope is being activated while thread_inherit_context "
                "is enabled (the default on the free-threaded build, e.g. 3.14t). "
                "A threading.Thread started while this scope is active inherits a "
                "copy of the parent context, so its .instance() calls resolve "
                "within this scope instead of falling through to the process-global "
                "registry. Activate a fresh scope inside each thread if you need "
                "per-thread isolation.",
                RuntimeWarning,
                stacklevel=3,
            )
        token = _active_scopes.set((*_active_scopes.get(), self))
        try:
            yield self
        finally:
            _active_scopes.reset(token)


class SingletonConfigurable(LoggingConfigurable):
    """A configurable that only allows one instance.

    This class is for classes that should only have one instance of itself
    or *any* subclass. To create and retrieve such a class use the
    :meth:`SingletonConfigurable.instance` method.
    """

    _instance = None

    @classmethod
    def _walk_mro(cls) -> t.Generator[type[SingletonConfigurable], None, None]:
        """Walk the cls.mro() for parent classes that are also singletons

        For use in instance()
        """

        for subclass in cls.mro():
            if (
                issubclass(cls, subclass)
                and issubclass(subclass, SingletonConfigurable)
                and subclass != SingletonConfigurable
            ):
                yield subclass

    @classmethod
    def scope(cls) -> SingletonScope:
        """Create a scope covering this class and its subclasses.

        The returned :class:`SingletonScope` is not active yet; call it to
        activate it for a dynamic extent (``with MyApp.scope()() as scope:``, or
        bind it first to inspect or re-enter it later). Called on
        ``SingletonConfigurable`` itself, it covers every singleton in the
        process.

        .. versionadded:: 5.17
        """
        return SingletonScope(cls)

    @classmethod
    def _current_scope(cls) -> SingletonScope | None:
        """The innermost active scope covering ``cls``, or None."""
        for scope in reversed(_active_scopes.get()):
            if scope.covers(cls):
                return scope
        return None

    @classmethod
    def clear_instance(cls) -> None:
        """unset _instance for this class and singleton parents.

        .. versionchanged:: 5.17
            Inside an active :class:`SingletonScope`, clears the scope's
            registry instead, leaving the process-global ``_instance`` alone.
        """
        scope = cls._current_scope()
        if scope is not None:
            for klass in list(scope._instances):
                if isinstance(scope._instances[klass], cls):
                    del scope._instances[klass]
            return
        if not cls.initialized():
            return
        for subclass in cls._walk_mro():
            if isinstance(subclass._instance, cls):
                # only clear instances that are instances
                # of the calling class
                subclass._instance = None  # type:ignore[unreachable]

    @classmethod
    def instance(cls, *args: t.Any, **kwargs: t.Any) -> Self:
        """Returns a global instance of this class.

        This method create a new instance if none have previously been created
        and returns a previously created instance is one already exists.

        The arguments and keyword arguments passed to this method are passed
        on to the :meth:`__init__` method of the class upon instantiation.

        Examples
        --------
        Create a singleton class using instance, and retrieve it::

            >>> from traitlets.config.configurable import SingletonConfigurable
            >>> class Foo(SingletonConfigurable): pass
            >>> foo = Foo.instance()
            >>> foo == Foo.instance()
            True

        Create a subclass that is retrieved using the base class instance::

            >>> class Bar(SingletonConfigurable): pass
            >>> class Bam(Bar): pass
            >>> bam = Bam.instance()
            >>> bam == Bar.instance()
            True

        .. versionchanged:: 5.17
            Inside an active :class:`SingletonScope`, resolves against that
            scope's registry — creating a fresh instance on first use — without
            reading, creating, or mutating the process-global ``_instance``.
        """
        scope = cls._current_scope()
        if scope is not None:
            if scope.get(cls) is None:
                inst = cls(*args, **kwargs)
                for subclass in cls._walk_mro():
                    scope._instances[subclass] = inst
            existing = scope.get(cls)
            if isinstance(existing, cls):
                return existing
            raise MultipleInstanceError(
                f"An incompatible sibling of '{cls.__name__}' is already instantiated"
                f" as singleton: {type(existing).__name__}"
            )

        # Create and save the instance
        if cls._instance is None:
            inst = cls(*args, **kwargs)
            # Now make sure that the instance will also be returned by
            # parent classes' _instance attribute.
            for subclass in cls._walk_mro():
                subclass._instance = inst

        if isinstance(cls._instance, cls):
            return cls._instance
        else:
            raise MultipleInstanceError(
                f"An incompatible sibling of '{cls.__module__}.{cls.__name__}' is already instantiated"
                f" as singleton: {type(cls._instance).__module__}.{type(cls._instance).__name__}"
            )

    @classmethod
    def initialized(cls) -> bool:
        """Has an instance been created?

        .. versionchanged:: 5.17
            Inside an active :class:`SingletonScope`, reports whether an
            instance exists *in that scope*, ignoring the process-global one.
        """
        scope = cls._current_scope()
        if scope is not None:
            return scope.get(cls) is not None
        return hasattr(cls, "_instance") and cls._instance is not None
