"""Tests for the opt-in observable container traits (ObservableList, ObservableDict)."""

import pytest

from traitlets import (
    HasTraits,
    Int,
    List,
    Dict,
    ObservableDict,
    ObservableList,
    TraitError,
    Unicode,
)
from traitlets.traitlets import _ObservableDict, _ObservableList


class ListModel(HasTraits):
    items = ObservableList(Int())


class DictModel(HasTraits):
    mapping = ObservableDict(value_trait=Int(), key_trait=Unicode())


def record(obj, name):
    events = []
    obj.observe(events.append, name)
    return events


# ----------------------------------------------------------------------------
# ObservableList
# ----------------------------------------------------------------------------


def test_list_value_is_list_subclass():
    m = ListModel()
    assert isinstance(m.items, _ObservableList)
    assert isinstance(m.items, list)


def test_list_default_values_are_independent():
    m1, m2 = ListModel(), ListModel()
    m1.items.append(1)
    assert m2.items == []


def test_list_append_notifies():
    m = ListModel()
    events = record(m, "items")
    m.items.append(1)
    assert m.items == [1]
    assert len(events) == 1
    change = events[0]
    assert change.type == "change"
    assert change.name == "items"
    assert change.owner is m
    assert change.operation == "append"
    assert change.index == 0
    assert change.old == []
    assert change.new is m.items


def test_list_mutation_operations_notify_with_positions():
    m = ListModel(items=[1, 2, 3])
    events = record(m, "items")

    m.items.extend([4, 5])
    m.items.insert(0, 0)
    m.items[1] = 9
    m.items[1:3] = [7, 8]
    del m.items[0]
    m.items.remove(8)
    m.items.pop()

    assert [e.operation for e in events] == [
        "extend",
        "insert",
        "setitem",
        "setitem",
        "delitem",
        "remove",
        "pop",
    ]
    assert events[0].index == slice(3, 5)
    assert events[1].index == 0
    assert events[2].index == 1
    assert events[3].index == slice(1, 3)
    assert events[4].index == 0
    assert events[5].index == 1
    assert events[6].index == 3
    assert events[3].old == [0, 9, 2, 3, 4, 5]
    assert m.items == [7, 3, 4]


def test_list_iadd_and_imul_notify():
    m = ListModel(items=[1, 2])
    events = record(m, "items")
    m.items += [3]
    m.items *= 2
    assert m.items == [1, 2, 3, 1, 2, 3]
    assert [e.operation for e in events] == ["extend", "imul"]


def test_list_append_invalid_element():
    m = ListModel(items=[1])
    events = record(m, "items")
    with pytest.raises(TraitError):
        m.items.append("not an int")
    assert m.items == [1]
    assert events == []


def test_list_extend_is_atomic():
    m = ListModel(items=[1])
    events = record(m, "items")
    with pytest.raises(TraitError):
        m.items.extend([2, "not an int", 3])
    assert m.items == [1]
    assert events == []


def test_list_slice_replacement_is_atomic():
    """A slice replacement containing an invalid element must leave
    both the value and the notifications untouched."""
    m = ListModel(items=[1, 2, 3])
    events = record(m, "items")
    with pytest.raises(TraitError):
        m.items[0:2] = [9, "not an int", 8]
    assert m.items == [1, 2, 3]
    assert events == []


def test_list_setitem_invalid_element():
    m = ListModel(items=[1, 2])
    events = record(m, "items")
    with pytest.raises(TraitError):
        m.items[0] = "not an int"
    assert m.items == [1, 2]
    assert events == []


def test_list_minlen_maxlen_enforced_on_mutation():
    class Bounded(HasTraits):
        items = ObservableList(Int(), minlen=1, maxlen=3)

    b = Bounded(items=[1, 2])
    events = record(b, "items")
    with pytest.raises(TraitError):
        b.items.extend([3, 4])
    with pytest.raises(TraitError):
        b.items *= 2
    assert b.items == [1, 2]
    assert events == []


def test_list_element_coercion_on_mutation():
    from traitlets import CInt

    class Coerced(HasTraits):
        items = ObservableList(CInt())

    c = Coerced()
    c.items.append("3")
    assert c.items == [3]


def test_list_rebind_detaches_from_old_owner():
    """Assigning the container to another instance moves ownership:
    further mutations notify the new owner, never the old one."""
    a, b = ListModel(), ListModel()
    a_events = record(a, "items")
    b_events = record(b, "items")

    a.items.append(1)
    b.items = a.items
    a_events.clear()
    b_events.clear()

    a.items.append(2)  # same container, now owned by b
    assert a_events == []
    assert len(b_events) == 1
    assert b_events[0].owner is b
    assert b.items == [1, 2]


def test_list_nested_containers_are_not_recursively_observed():
    class Nested(HasTraits):
        matrix = ObservableList(ObservableList(Int()))

    n = Nested(matrix=[[1, 2]])
    events = record(n, "matrix")
    inner = n.matrix[0]
    assert isinstance(inner, _ObservableList)
    inner.append(3)  # validated, but not observed by the outer trait
    assert n.matrix == [[1, 2, 3]]
    assert events == []
    with pytest.raises(TraitError):
        inner.append("not an int")


def test_list_plain_list_trait_is_not_observable():
    """Plain List traits must not change behavior."""

    class Plain(HasTraits):
        items = List(Int())

    p = Plain()
    events = record(p, "items")
    p.items.append(1)
    assert type(p.items) is list
    assert events == []


# ----------------------------------------------------------------------------
# ObservableDict
# ----------------------------------------------------------------------------


def test_dict_value_is_dict_subclass():
    m = DictModel()
    assert isinstance(m.mapping, _ObservableDict)
    assert isinstance(m.mapping, dict)


def test_dict_setitem_notifies():
    m = DictModel()
    events = record(m, "mapping")
    m.mapping["a"] = 1
    assert m.mapping == {"a": 1}
    assert len(events) == 1
    change = events[0]
    assert change.type == "change"
    assert change.operation == "setitem"
    assert change.key == "a"
    assert change.old == {}
    assert change.new is m.mapping


def test_dict_mutation_operations_notify_with_keys():
    m = DictModel(mapping={"a": 1, "b": 2})
    events = record(m, "mapping")

    m.mapping.update({"c": 3})
    del m.mapping["a"]
    m.mapping.pop("b")
    m.mapping.setdefault("d", 4)

    assert [e.operation for e in events] == ["update", "delitem", "pop", "setdefault"]
    assert events[0].key == ["c"]
    assert events[1].key == "a"
    assert events[2].key == "b"
    assert events[3].key == "d"
    assert events[1].old == {"a": 1, "b": 2, "c": 3}
    assert m.mapping == {"c": 3, "d": 4}


def test_dict_setitem_invalid_value():
    m = DictModel(mapping={"a": 1})
    events = record(m, "mapping")
    with pytest.raises(TraitError):
        m.mapping["b"] = "not an int"
    assert m.mapping == {"a": 1}
    assert events == []


def test_dict_invalid_key():
    m = DictModel()
    events = record(m, "mapping")
    with pytest.raises(TraitError):
        m.mapping[1] = 1
    assert m.mapping == {}
    assert events == []


def test_dict_update_is_atomic():
    """An update containing an invalid entry must leave both the value
    and the notifications untouched."""
    m = DictModel(mapping={"a": 1})
    events = record(m, "mapping")
    with pytest.raises(TraitError):
        m.mapping.update({"b": 2, "c": "not an int"})
    assert m.mapping == {"a": 1}
    assert events == []


def test_dict_ior_is_atomic():
    m = DictModel(mapping={"a": 1})
    events = record(m, "mapping")
    with pytest.raises(TraitError):
        m.mapping |= {"b": "not an int"}
    assert m.mapping == {"a": 1}
    assert events == []


def test_dict_setdefault_existing_key_does_not_notify():
    m = DictModel(mapping={"a": 1})
    events = record(m, "mapping")
    assert m.mapping.setdefault("a", 99) == 1
    assert m.mapping == {"a": 1}
    assert events == []


def test_dict_pop_missing_key_does_not_notify():
    m = DictModel()
    events = record(m, "mapping")
    assert m.mapping.pop("missing", 42) == 42
    with pytest.raises(KeyError):
        m.mapping.pop("missing")
    assert events == []


def test_dict_per_key_traits_validated_on_mutation():
    class PerKey(HasTraits):
        mapping = ObservableDict(per_key_traits={"n": Int(), "s": Unicode()})

    p = PerKey()
    p.mapping["n"] = 1
    with pytest.raises(TraitError):
        p.mapping["n"] = "not an int"
    assert p.mapping == {"n": 1}


def test_dict_rebind_detaches_from_old_owner():
    a, b = DictModel(), DictModel()
    a_events = record(a, "mapping")
    b_events = record(b, "mapping")

    a.mapping["x"] = 1
    b.mapping = a.mapping
    a_events.clear()
    b_events.clear()

    a.mapping["y"] = 2  # same container, now owned by b
    assert a_events == []
    assert len(b_events) == 1
    assert b_events[0].owner is b
    assert b.mapping == {"x": 1, "y": 2}


def test_dict_plain_dict_trait_is_not_observable():
    """Plain Dict traits must not change behavior."""

    class Plain(HasTraits):
        mapping = Dict()

    p = Plain()
    events = record(p, "mapping")
    p.mapping["a"] = 1
    assert type(p.mapping) is dict
    assert events == []


# ----------------------------------------------------------------------------
# hold_trait_notifications interaction
# ----------------------------------------------------------------------------


def test_mutation_events_are_not_merged_in_hold():
    """Each in-place mutation event is queued individually and delivered
    in order when the hold context exits."""
    m = ListModel()
    events = record(m, "items")
    with m.hold_trait_notifications():
        m.items.append(1)
        m.items.append(2)
        m.items[0] = 9
    assert [e.operation for e in events] == ["append", "append", "setitem"]
    assert [e.index for e in events] == [0, 1, 0]


def test_plain_assignment_still_merges_in_hold():
    m = ListModel()
    events = record(m, "items")
    with m.hold_trait_notifications():
        m.items = [1]
        m.items = [2, 3]
    # plain assignments merge into a single notification with the latest value
    assert len(events) == 1
    assert "operation" not in events[0]
    assert events[0].new == [2, 3]


def test_mutation_and_assignment_do_not_swallow_each_other_in_hold():
    m = ListModel()
    events = record(m, "items")
    with m.hold_trait_notifications():
        m.items = [1]
        m.items.append(2)
        m.items = [3]
    assert len(events) == 3
    assert events[1].operation == "append"
    assert m.items == [3]


def test_dict_mutation_events_in_hold():
    m = DictModel()
    events = record(m, "mapping")
    with m.hold_trait_notifications():
        m.mapping["a"] = 1
        m.mapping.update({"b": 2})
    assert [e.operation for e in events] == ["setitem", "update"]
