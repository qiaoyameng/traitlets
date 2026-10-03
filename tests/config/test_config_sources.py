"""Tests for optional config-value provenance (source tracking)."""

from __future__ import annotations

import os
from tempfile import TemporaryDirectory

import pytest

from traitlets import Dict, Integer, List, Set, Unicode
from traitlets.config import Config, Configurable, SourceRecord
from traitlets.config.loader import (
    JSONFileConfigLoader,
    KVArgParseConfigLoader,
    PyFileConfigLoader,
    SOURCE_COMMAND_LINE,
    SOURCE_DEFAULT,
    SOURCE_ENVIRONMENT,
    SOURCE_UNKNOWN,
)


BASE_CONFIG = """
c = get_config()
c.MyApp.name = "from-base"
c.MyApp.handlers.append("base-handler")
c.MyApp.options.update({"a": 1})
"""

USER_CONFIG = """
c = get_config()
c.MyApp.name = "from-user"
c.MyApp.handlers.append("user-handler")
c.MyApp.options.update({"b": 2})
"""


class MyApp(Configurable):
    name = Unicode("default-name", config=True)
    handlers = List(default_value=["seed"], config=True)
    options = Dict(default_value={"seed_k": 0}, config=True)


def _load_three_layers(tmpdir):
    with open(os.path.join(tmpdir, "base_config.py"), "w") as f:
        f.write(BASE_CONFIG)
    with open(os.path.join(tmpdir, "user_config.py"), "w") as f:
        f.write(USER_CONFIG)

    base = PyFileConfigLoader("base_config.py", path=tmpdir).load_config()
    user = PyFileConfigLoader("user_config.py", path=tmpdir).load_config()
    cli = KVArgParseConfigLoader(
        argv=["--MyApp.name=from-cli", "--MyApp.handlers=cli-h"],
        classes=[MyApp],
    ).load_config()

    merged = Config(source=None)
    merged.merge(base)
    merged.merge(user)
    merged.merge(cli)
    return merged, tmpdir


def test_records_hold_only_labels():
    record = SourceRecord("/path/to/file.py", "append")
    assert record.source == "/path/to/file.py"
    assert record.operation == "append"
    # provenance never stores config values, so secrets cannot leak here
    assert not hasattr(record, "value")


def test_plain_config_is_unknown():
    cfg = Config({"Foo": {"bar": 1}})
    trace = cfg.get_trace("Foo.bar")
    assert trace is not None
    assert trace.records == [SourceRecord(SOURCE_UNKNOWN, "set")]


def test_nested_section_tracking():
    cfg = Config(source="nested-file")
    cfg.Foo.Bar.value = 7
    assert cfg.get_trace("Foo.Bar.value").records == [
        SourceRecord("nested-file", "set")
    ]


def test_lazy_append_prepend_insert_tags():
    cfg = Config(source="file")
    cfg.W.lst.append("a")
    cfg.W.lst.prepend(["p"])
    cfg.W.lst.insert(0, "i")

    value, sources = cfg.W.lst.resolve(["base"])
    assert value == ["p", "i", "base", "a"]
    assert [tag.operation for tag in sources] == [
        "prepend",
        "insert",
        "default",
        "append",
    ]
    assert {tag.source for tag in sources} == {"file", SOURCE_DEFAULT}


def test_lazy_dict_update_tags():
    cfg = Config(source="file")
    cfg.W.d.update({"a": 1, "b": 2})
    value, sources = cfg.W.d.resolve({"a": 0, "z": 26})
    assert value == {"a": 1, "b": 2, "z": 26}
    assert sources["a"] == SourceRecord("file", "update")
    assert sources["b"] == SourceRecord("file", "update")
    assert sources["z"] == SourceRecord(SOURCE_DEFAULT, "default")


def test_lazy_set_tags():
    class H(Configurable):
        bag = Set(default_value={"x"}, config=True)

    cfg = Config(source="file")
    cfg.H.bag.add("y")
    value, sources = cfg.H.bag.resolve({"x"})
    assert value == {"x", "y"}
    assert sources["x"] == SourceRecord(SOURCE_DEFAULT, "default")
    assert sources["y"] == SourceRecord("file", "update")


def test_lazy_merge_keeps_chained_tags():
    c1 = Config(source="file-a")
    c1.Foo.lst.append(1)
    c2 = Config(source="file-b")
    c2.Foo.lst.append(2)

    merged = Config(source=None)
    merged.merge(c1)
    merged.merge(c2)

    value, sources = merged.Foo.lst.resolve([])
    assert value == [1, 2]
    assert [tag.source for tag in sources] == ["file-a", "file-b"]
    assert [tag.operation for tag in sources] == ["append", "append"]


def test_two_files_and_command_line_merge(tmp_path):
    merged, tmpdir = _load_three_layers(str(tmp_path))

    trace = merged.get_trace("MyApp.name")
    assert [record.source for record in trace.records] == [
        os.path.join(tmpdir, "base_config.py"),
        os.path.join(tmpdir, "user_config.py"),
        SOURCE_COMMAND_LINE,
    ]

    app = MyApp(config=merged)

    # scalar: command line wins, full override chain retained
    assert app.name == "from-cli"
    assert app.config_source("name") == SOURCE_COMMAND_LINE

    # single --List=x assignment clobbers (traitlets semantics),
    # but the file append history stays visible in the chain
    assert app.handlers == ["cli-h"]
    handler_records = app.config_source_records("handlers")
    assert [r.operation for r in handler_records[:2]] == ["append", "append"]
    assert handler_records[-1].source == SOURCE_COMMAND_LINE
    assert app.config_element_sources("handlers") == [
        SourceRecord(SOURCE_COMMAND_LINE, "set")
    ]

    # dict: updates from both files compose, defaults are distinguishable
    assert app.options == {"seed_k": 0, "a": 1, "b": 2}
    assert app.config_element_sources("options") == {
        "seed_k": SourceRecord(SOURCE_DEFAULT, "default"),
        "a": SourceRecord(
            os.path.join(tmpdir, "base_config.py"), "update"
        ),
        "b": SourceRecord(
            os.path.join(tmpdir, "user_config.py"), "update"
        ),
    }


def test_command_line_repeated_flags(tmp_path):
    with open(os.path.join(tmp_path, "base_config.py"), "w") as f:
        f.write("c = get_config()\nc.MyApp.handlers.append('file-h')\n")
    base = PyFileConfigLoader("base_config.py", path=str(tmp_path)).load_config()
    cli = KVArgParseConfigLoader(
        argv=["--MyApp.handlers=cli-1", "--MyApp.handlers=cli-2"],
        classes=[MyApp],
    ).load_config()
    merged = Config(source=None)
    merged.merge(base)
    merged.merge(cli)

    app = MyApp(config=merged)
    assert app.handlers == ["cli-1", "cli-2"]
    assert [r.source for r in app.config_element_sources("handlers")] == [
        SOURCE_COMMAND_LINE,
        SOURCE_COMMAND_LINE,
    ]


def test_json_loader_uses_file_path():
    with TemporaryDirectory() as td:
        path = os.path.join(td, "cfg.json")
        with open(path, "w") as f:
            f.write('{"version": 1, "MyApp": {"name": "json"}}')
        loaded = JSONFileConfigLoader("cfg.json", path=td).load_config()
        assert loaded.get_trace("MyApp.name").records == [
            SourceRecord(path, "set")
        ]


def test_inherited_class_sections():
    class Base(Configurable):
        common = Unicode("d", config=True)

    class Sub(Base):
        extra = Integer(0, config=True)

    cfg = Config(source="file")
    cfg.Base.common = "base"
    cfg.Sub.common = "sub"
    cfg.Sub.extra = 42

    sub = Sub(config=cfg)
    assert sub.common == "sub"
    assert [r.source for r in sub.config_source_records("common")] == [
        "file",
        "file",
    ]
    assert sub.config_source("extra") == "file"


def test_default_source_without_config():
    app = MyApp()
    assert app.config_source("name") == SOURCE_DEFAULT
    assert app.config_source_records("name") == [
        SourceRecord(SOURCE_DEFAULT, "default")
    ]
    assert app.config_element_sources("handlers") == [
        SourceRecord(SOURCE_DEFAULT, "default")
    ]
    assert app.config_element_sources("options") == {
        "seed_k": SourceRecord(SOURCE_DEFAULT, "default")
    }


def test_constructor_kwarg_source():
    app = MyApp(name="kwarg")
    assert app.config_source("name") == "constructor"
    assert app.name == "kwarg"


def test_unknown_trait_query_raises():
    app = MyApp()
    with pytest.raises(AttributeError):
        app.config_source("not_a_trait")


def test_explain_hides_value_by_default():
    app = MyApp()
    explanation = app.explain_config_value("name")
    assert "default-name" not in explanation
    assert SOURCE_DEFAULT in explanation
    assert app.explain_config_value("name", include_value=True) == (
        explanation + "\n  value: 'default-name'"
    )


def test_explain_does_not_reexecute_files(tmp_path):
    # explanation works from recorded metadata after the file is gone
    path = os.path.join(tmp_path, "base_config.py")
    with open(path, "w") as f:
        f.write("c = get_config()\nc.MyApp.name = 'x'\n")
    loaded = PyFileConfigLoader("base_config.py", path=str(tmp_path)).load_config()
    app = MyApp(config=loaded)
    os.remove(path)
    explanation = app.explain_config_value("name")
    assert "base_config.py" in explanation
    assert "'x'" not in explanation


def test_source_tracking_survives_copy_and_deepcopy():
    import copy

    cfg = Config(source="file")
    cfg.Foo.bar = 1
    assert copy.copy(cfg).get_trace("Foo.bar").records == [
        SourceRecord("file", "set")
    ]
    assert copy.deepcopy(cfg).get_trace("Foo.bar").records == [
        SourceRecord("file", "set")
    ]


def test_pickle_preserves_provenance():
    import pickle

    cfg = Config(source="file")
    cfg.Foo.bar = 1
    restored = pickle.loads(pickle.dumps(cfg))
    assert restored == cfg
    assert restored.get_trace("Foo.bar").records == [
        SourceRecord("file", "set")
    ]


def test_source_label_constants():
    assert SOURCE_DEFAULT == "default"
    assert SOURCE_UNKNOWN == "unknown"
    assert SOURCE_COMMAND_LINE == "command-line"
    assert SOURCE_ENVIRONMENT == "environment"
