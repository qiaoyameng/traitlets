from __future__ import annotations

import pytest

from traitlets import Computed, HasTraits, Int, TraitError, Unicode, computed, observe


def test_computed_trait_is_read_only_and_lazily_cached() -> None:
    class Model(HasTraits):
        source = Int()
        calls = 0

        @Computed("source", trait=Int())
        def result(self) -> int:
            self.calls += 1
            return self.source + 1

    model = Model()
    assert model.calls == 0

    assert model.result == 1
    assert model.result == 1
    assert model.calls == 1

    model.source = 2
    assert model.result == 3
    assert model.calls == 2

    with pytest.raises(TraitError, match="read-only"):
        model.result = 4
    with pytest.raises(TraitError, match="read-only"):
        model.set_trait("result", 4)


def test_missing_dependency_is_rejected() -> None:
    with pytest.raises(TraitError, match="undefined trait"):
        class MissingDependency(HasTraits):
            source = Int()

            @computed("missing")
            def result(self) -> int:
                return self.source


def test_circular_dependency_is_rejected() -> None:
    with pytest.raises(TraitError, match="Circular dependency"):
        class Circular(HasTraits):
            @computed("other")
            def one(self) -> int:
                return self.other

            @computed("one")
            def other(self) -> int:
                return self.one


def test_compute_error_is_not_cached_and_can_be_retried() -> None:
    class Model(HasTraits):
        source = Int()
        fail = True
        calls = 0

        @computed("source", trait=Int())
        def result(self) -> int:
            self.calls += 1
            if self.fail:
                raise ValueError("boom")
            return self.source

    model = Model()
    with pytest.raises(ValueError, match="boom"):
        model.result
    with pytest.raises(ValueError, match="boom"):
        model.result
    assert model.calls == 2
    assert "result" not in model._trait_values

    model.fail = False
    assert model.result == 0
    assert model.calls == 3


def test_instances_have_independent_caches() -> None:
    class Model(HasTraits):
        source = Int()

        @computed("source", trait=Int())
        def result(self) -> int:
            return self.source + 1

    first = Model()
    second = Model(source=10)

    assert first.result == 1
    assert second.result == 11
    first.source = 5
    assert first.result == 6
    assert second.result == 11


def test_derived_traits_can_be_dependencies() -> None:
    class Model(HasTraits):
        source = Int()
        calls: dict[str, int] = {}

        @computed("source", trait=Int())
        def middle(self) -> int:
            self.calls["middle"] = self.calls.get("middle", 0) + 1
            return self.source + 1

        @computed("middle", trait=Int())
        def result(self) -> int:
            self.calls["result"] = self.calls.get("result", 0) + 1
            return self.middle * 2

    model = Model()
    assert model.result == 2
    assert model.calls == {"middle": 1, "result": 1}

    model.source = 2
    assert model.result == 6
    assert model.calls == {"middle": 2, "result": 2}


def test_batched_changes_compute_diamond_dependency_once() -> None:
    class Diamond(HasTraits):
        left = Int()
        right = Int()
        calls: dict[str, int] = {}

        @computed("left", "right", trait=Int())
        def common(self) -> int:
            self.calls["common"] = self.calls.get("common", 0) + 1
            return self.left + self.right

        @computed("common", trait=Int())
        def upper(self) -> int:
            self.calls["upper"] = self.calls.get("upper", 0) + 1
            return self.common

        @computed("common", trait=Int())
        def lower(self) -> int:
            self.calls["lower"] = self.calls.get("lower", 0) + 1
            return self.common

        @computed("upper", "lower", trait=Int())
        def result(self) -> int:
            self.calls["result"] = self.calls.get("result", 0) + 1
            return self.upper + self.lower

    model = Diamond()
    assert model.result == 0
    model.calls.clear()

    with model.hold_trait_notifications():
        model.left = 2
        model.right = 3

    assert model.result == 10
    assert model.calls == {"common": 1, "upper": 1, "lower": 1, "result": 1}


def test_inherited_override_uses_final_class_definition() -> None:
    class Base(HasTraits):
        source = Int()

        @computed("source", trait=Int())
        def result(self) -> int:
            return self.source + 1

    class Child(Base):
        source = Unicode()

        @computed("source", trait=Unicode())
        def result(self) -> str:
            return self.source + "!"

    assert Base(source=1).result == 2
    assert Child(source="ready").result == "ready!"
    with pytest.raises(TraitError):
        Child(source=1).result


def test_computed_change_notification_uses_latest_value() -> None:
    class Model(HasTraits):
        source = Int()
        events = []

        @computed("source", trait=Int())
        def result(self) -> int:
            return self.source * 2

        @observe("result")
        def result_changed(self, change) -> None:
            self.events.append((change.old, change.new))

    model = Model()
    assert model.result == 0
    with model.hold_trait_notifications():
        model.source = 2
        model.source = 3
    assert model.result == 6
    assert model.events == [(0, 6)]


def test_dynamically_added_computed_trait_validates_dependencies() -> None:
    class Model(HasTraits):
        source = Int()

    model = Model()
    model.add_traits(
        result=Computed("source", compute=lambda obj: obj.source + 1, trait=Int())
    )
    assert model.result == 1
    model.source = 2
    assert model.result == 3
