"""What building a URI path costs: one scan of the installed plugins however
many unknown schemes are asked for, and a slot list worked out once per
class."""

from __future__ import annotations

from unittest import mock

import pytest

pytest.importorskip("uritools")

from pathlib_next import uri as uri_module
from pathlib_next.uri import Uri, UriPath

# --- the entry points are read once -----------------------------------------------------


def test_many_unknown_schemes_scan_the_plugins_once_and_remember_nothing_per_scheme():
    calls = []

    def entry_points(*args, **kwargs):
        calls.append(kwargs)
        return [] if kwargs.get("group") else {}

    with mock.patch("importlib.metadata.entry_points", side_effect=entry_points):
        for index in range(300):
            assert type(UriPath(f"nosuch{index}-g5:x")) is UriPath
        declared = uri_module._ENTRY_POINTS[1]
    assert len(calls) == 1
    assert declared == {}


def test_an_entry_point_that_registers_nothing_is_loaded_once():
    ep = mock.Mock()
    ep.name = "declaredg5"
    ep.load.return_value = object
    with mock.patch("importlib.metadata.entry_points", return_value=[ep]) as lookup:
        for _ in range(5):
            assert type(UriPath("declaredg5://h/x")) is UriPath
    assert ep.load.call_count == 1
    assert lookup.call_count == 1


def test_a_class_defined_later_makes_the_plugins_be_read_again():
    first = mock.Mock(name="first")
    first.name = "laterg5"
    first.load.return_value = object
    with mock.patch("importlib.metadata.entry_points", return_value=[first]) as lookup:
        UriPath("laterg5://h/x")

        class Defined(UriPath):  # noqa: F841 - defining it is the point
            __SCHEMES = ("definedg5",)
            __slots__ = ()

        UriPath("laterg5://h/y")
        assert lookup.call_count == 2


# --- slots --------------------------------------------------------------------------------


def test_a_new_instance_starts_with_every_slot_at_none():
    for cls in (Uri, UriPath):
        instance = Uri.__new__(cls)
        names = cls._slot_names
        assert "_raw_uris" in names and "_initiated" in names
        assert len(names) == len(set(names))
        assert all(getattr(instance, name) is None for name in names)
    assert "_backend" in UriPath._slot_names
    assert "_backend" not in Uri._slot_names


def test_a_subclass_gets_its_own_slots_in_the_list():
    class WithSlot(UriPath):
        __SCHEMES = ("withslotg5",)
        __slots__ = ("_extra",)

    assert "_extra" in WithSlot._slot_names
    assert "_extra" not in UriPath._slot_names
    instance = UriPath("withslotg5://h/x")
    assert instance._extra is None
    assert type(instance) is WithSlot


def test_a_class_with_a_string_slot_or_a_weakref_slot_is_constructed():
    class Odd(UriPath):
        __SCHEMES = ("oddg5",)
        __slots__ = ("_single", "__weakref__")

    instance = UriPath("oddg5://h/x")
    assert instance._single is None
    assert Odd._slot_names.count("_single") == 1
    assert "__weakref__" not in Odd._slot_names
