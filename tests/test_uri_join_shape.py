"""What joining a key costs: one construction unless the scheme gives its
children a meaning of its own, in which case it is asked for every name."""

from __future__ import annotations

import pytest

pytest.importorskip("uritools")

from pathlib_next.uri import UriPath


class CountingPath(UriPath):
    """Counts the instances built through `_from_parsed_parts`."""

    __SCHEMES = ("countg5",)
    __slots__ = ()
    built = 0

    def _from_parsed_parts(self, *args, **kwargs):
        type(self).built += 1
        return super()._from_parsed_parts(*args, **kwargs)


class NamedPath(CountingPath):
    """A scheme that gives names a meaning of its own."""

    __SCHEMES = ("namedg5",)
    __slots__ = ()
    asked: list = []

    def _make_child_relpath(self, name, **kwargs):
        type(self).asked.append(name)
        return super()._make_child_relpath(name, **kwargs)


# --- a joined key is written once ----------------------------------------------------


def test_a_str_join_builds_one_path_whatever_the_number_of_segments():
    base = UriPath("countg5://h/root")
    CountingPath.built = 0
    joined = base / "a/b/c/d/e"
    assert CountingPath.built == 1
    assert joined.as_uri() == "countg5://h/root/a/b/c/d/e"
    CountingPath.built = 0
    assert (base / "x//y/").as_uri() == "countg5://h/root/x//y/"
    assert CountingPath.built == 1
    CountingPath.built = 0
    assert (base / "/abs/p").as_uri() == "countg5://h/abs/p"
    assert CountingPath.built == 1


def test_joinpath_builds_one_path_per_argument():
    base = UriPath("countg5://h/root")
    CountingPath.built = 0
    assert base.joinpath("a/b", "c/d/e").as_uri() == "countg5://h/root/a/b/c/d/e"
    assert CountingPath.built == 2


def test_a_scheme_that_builds_its_own_children_is_asked_for_every_name():
    base = UriPath("namedg5://h/root")
    NamedPath.asked.clear()
    assert (base / "a/b/c").as_uri() == "namedg5://h/root/a/b/c"
    assert NamedPath.asked == ["a", "b", "c"]
    NamedPath.asked.clear()
    assert (base / "/x//y/").as_uri() == "namedg5://h/x//y/"
    assert NamedPath.asked == ["x", "y"]


def test_a_listing_still_builds_each_child_through_the_child_hook(tmp_path):
    for name in ("one", "two"):
        (tmp_path / name).write_text("x")
    seen = []
    base = UriPath(tmp_path.as_uri())

    class Spy(type(base)):
        __slots__ = ()

        def _make_child_relpath(self, name, **kwargs):
            seen.append(name)
            return super()._make_child_relpath(name, **kwargs)

    listing = Spy(tmp_path.as_uri())
    assert sorted(child.name for child in listing.iterdir()) == ["one", "two"]
    assert sorted(seen) == ["one", "two"]


def test_the_written_path_is_the_one_a_child_by_child_join_gives():
    base = UriPath("countg5://h/root")
    named = UriPath("namedg5://h/root")
    for key in ("a", "a/b", "a//b", "a/", "a//", "/", "/a", "/a//b/", "a/b/c/d"):
        left = (base / key).path
        right = (named / key).path
        assert left == right, key
