"""Tests for config provenance (source tracking)."""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
from __future__ import annotations

import json

from traitlets import Dict, Integer, List, Unicode
from traitlets.config import Configurable
from traitlets.config.loader import (
    COMMAND_LINE_SOURCE,
    DEFAULT_SOURCE,
    UNKNOWN_SOURCE,
    Config,
    JSONFileConfigLoader,
    KVArgParseConfigLoader,
    PyFileConfigLoader,
)


class Foo(Configurable):
    tags = List(Unicode(), default_value=["default-tag"]).tag(config=True)
    count = Integer(0).tag(config=True)
    name = Unicode("default-name").tag(config=True)
    mapping = Dict().tag(config=True)


class BaseFoo(Configurable):
    count = Integer(0).tag(config=True)


class ChildFoo(BaseFoo):
    pass


def _sources(records):
    return [(r.source, r.operation) for r in records]


def test_merge_two_files_and_command_line(tmp_path):
    """Merge two file layers and one command-line layer.

    Check final values and the per-key source chain, in order.
    """
    (tmp_path / "base.json").write_text(
        json.dumps({"Foo": {"tags": ["a"], "count": 1, "name": "from-json"}})
    )
    (tmp_path / "extra.py").write_text(
        "c.Foo.tags.append('b')\nc.Foo.tags.prepend(['z'])\nc.Foo.count = 2\n"
    )

    json_loader = JSONFileConfigLoader("base.json", path=str(tmp_path))
    json_config = json_loader.load_config()
    py_loader = PyFileConfigLoader("extra.py", path=str(tmp_path))
    py_config = py_loader.load_config()
    cli_config = KVArgParseConfigLoader(
        ["--Foo.count=3", "--Foo.name=from-cli"], classes=[Foo]
    ).load_config()

    config = Config()
    config.merge(json_config)
    config.merge(py_config)
    config.merge(cli_config)

    foo = Foo(config=config)

    # final values: list extended by the py file, scalars overridden by CLI
    assert foo.tags == ["z", "a", "b"]
    assert foo.count == 3
    assert foo.name == "from-cli"

    json_source = f"file:{json_loader.full_filename}"
    py_source = f"file:{py_loader.full_filename}"

    # source chains, item by item, lowest to highest priority
    explained = foo.explain_config()
    assert _sources(explained["tags"]) == [
        (json_source, "set"),
        (py_source, "prepend"),
        (py_source, "append"),
    ]
    assert _sources(explained["count"]) == [
        (json_source, "set"),
        (py_source, "set"),
        (COMMAND_LINE_SOURCE, "set"),
    ]
    assert _sources(explained["name"]) == [
        (json_source, "set"),
        (COMMAND_LINE_SOURCE, "set"),
    ]

    # final source of each trait
    assert foo.config_source("tags") == py_source
    assert foo.config_source("count") == COMMAND_LINE_SOURCE
    assert foo.config_source("name") == COMMAND_LINE_SOURCE
    # untouched by any config: the trait default
    assert foo.config_source("mapping") == DEFAULT_SOURCE

    # the same chains are visible on the merged Config itself
    assert _sources(config.sources_for("Foo.count")) == _sources(explained["count"])
    assert _sources(config.sources_for("Foo.tags")) == _sources(explained["tags"])


def test_explain_does_not_reexecute_config_files(tmp_path):
    """explain_config uses recorded provenance only, never re-reads files."""
    py_file = tmp_path / "app.py"
    py_file.write_text("c.Foo.count = 5\n")
    loader = PyFileConfigLoader("app.py", path=str(tmp_path))
    config = loader.load_config()
    # any attempt to re-read or re-execute the file would now fail
    py_file.unlink()

    foo = Foo(config=config)
    assert foo.count == 5
    source = f"file:{loader.full_filename}"
    assert foo.config_source("count") == source
    assert _sources(foo.explain_config()["count"]) == [(source, "set")]


def test_plain_config_has_unknown_source():
    """A plain user-constructed Config gets an explicit unknown marker."""
    foo = Foo(config=Config({"Foo": {"count": 7}}))
    assert foo.count == 7
    assert _sources(foo.explain_config()["count"]) == [(UNKNOWN_SOURCE, "set")]
    assert foo.config_source("count") == UNKNOWN_SOURCE
    assert foo.config_source("name") == DEFAULT_SOURCE


def test_inherited_class_sections_provenance(tmp_path):
    """The source chain follows config inherited from base-class sections."""
    (tmp_path / "base.json").write_text(json.dumps({"BaseFoo": {"count": 2}}))
    (tmp_path / "child.json").write_text(json.dumps({"ChildFoo": {"count": 4}}))

    base_loader = JSONFileConfigLoader("base.json", path=str(tmp_path))
    child_loader = JSONFileConfigLoader("child.json", path=str(tmp_path))
    config = Config()
    config.merge(base_loader.load_config())
    config.merge(child_loader.load_config())

    child = ChildFoo(config=config)
    assert child.count == 4
    assert _sources(child.explain_config()["count"]) == [
        (f"file:{base_loader.full_filename}", "set"),
        (f"file:{child_loader.full_filename}", "set"),
    ]
    assert child.config_source("count") == f"file:{child_loader.full_filename}"


def test_lazy_update_provenance(tmp_path):
    """LazyConfigValue update operations are recorded as such."""
    (tmp_path / "extra.py").write_text("c.Foo.mapping.update({'added': 1})\n")
    loader = PyFileConfigLoader("extra.py", path=str(tmp_path))
    foo = Foo(config=loader.load_config())

    assert foo.mapping == {"added": 1}
    assert _sources(foo.explain_config()["mapping"]) == [
        (f"file:{loader.full_filename}", "update")
    ]


def test_update_config_appends_provenance(tmp_path):
    """Later update_config calls extend the recorded chain."""
    (tmp_path / "base.json").write_text(json.dumps({"Foo": {"count": 1}}))
    json_loader = JSONFileConfigLoader("base.json", path=str(tmp_path))
    foo = Foo(config=json_loader.load_config())

    cli_config = KVArgParseConfigLoader(["--Foo.count=3"], classes=[Foo]).load_config()
    foo.update_config(cli_config)

    assert foo.count == 3
    assert _sources(foo.explain_config()["count"]) == [
        (f"file:{json_loader.full_filename}", "set"),
        (COMMAND_LINE_SOURCE, "set"),
    ]


def test_load_subconfig_keeps_own_source(tmp_path):
    """Sub-configs loaded from another file keep their own source label."""
    (tmp_path / "sub.py").write_text("c.Foo.count = 9\n")
    (tmp_path / "main.py").write_text("c.Foo.name = 'main'\nload_subconfig('sub.py')\n")

    loader = PyFileConfigLoader("main.py", path=str(tmp_path))
    config = loader.load_config()
    foo = Foo(config=config)

    assert foo.count == 9
    assert foo.name == "main"
    main_source = f"file:{loader.full_filename}"
    sub_source = f"file:{tmp_path}/sub.py"
    assert _sources(foo.explain_config()["count"]) == [(sub_source, "set")]
    assert _sources(foo.explain_config()["name"]) == [(main_source, "set")]


def test_provenance_survives_deepcopy(tmp_path):
    """Copying a config (as Application does) preserves provenance."""
    import copy

    (tmp_path / "base.json").write_text(json.dumps({"Foo": {"count": 1}}))
    loader = JSONFileConfigLoader("base.json", path=str(tmp_path))
    config = copy.deepcopy(loader.load_config())

    foo = Foo(config=config)
    assert _sources(foo.explain_config()["count"]) == [
        (f"file:{loader.full_filename}", "set")
    ]
