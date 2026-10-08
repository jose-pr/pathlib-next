"""An empty segment inside a joined key is a segment (RFC 3986 3.3), whichever
way the join is spelled: a `str` key, a `Uri` argument, the constructor or
the finished URI text give the same URI."""

from __future__ import annotations

import pytest

pytest.importorskip("uritools")

from pathlib_next.uri import Uri, UriPath

BASES = [
    "http://h/d",
    "http://h/d/",
    "http://h",
    "http://h/",
    "s3://bucket/prefix",
    "sftp://u@h:22/srv",
    "file:///tmp/d",
    "a",
    "a/",
    "/",
    "/abs",
]
KEYS = ["x//y", "x///y", "x//", "x//y/", "a/b//c/d", "p//q//r"]


@pytest.mark.parametrize("base", BASES)
@pytest.mark.parametrize("key", KEYS)
def test_the_four_spellings_of_a_join_agree(base, key):
    by_str = Uri(base) / key
    by_joinpath = Uri(base).joinpath(key)
    by_uri = Uri(base) / Uri(key)
    by_constructor = Uri(base, key)
    text = by_str.as_uri()
    assert by_joinpath.as_uri() == by_uri.as_uri() == by_constructor.as_uri() == text
    assert "//" in by_str.path[1:], by_str.path
    assert Uri(text).path == by_str.path
    assert by_str.segments == by_constructor.segments
    path_base = UriPath(base)
    assert (path_base / key).as_uri() == text
    assert path_base.joinpath("", key).as_uri() == text


def test_the_empty_segment_is_part_of_the_name_and_the_parent():
    joined = Uri("http://h/d") / "x//y"
    assert joined.as_uri() == "http://h/d/x//y"
    assert joined.segments == ("", "d", "x", "", "y")
    assert joined.name == "y"
    assert joined.parent.as_uri() == "http://h/d/x/"
    assert joined.parent.parent.as_uri() == "http://h/d/x"


def test_a_key_with_an_empty_segment_is_a_different_object_key():
    plain = UriPath("s3://bucket/d") / "y.txt"
    doubled = UriPath("s3://bucket/d") / "/y.txt"
    nested = UriPath("s3://bucket") / "d//y.txt"
    assert nested != plain and nested != doubled
    assert nested.key == "d//y.txt"
    assert plain.key == "d/y.txt"


def test_an_absolute_key_restarts_and_keeps_its_empty_segments():
    base = Uri("http://h/d/e")
    assert (base / "/x//y").path == "/x//y"
    assert (base / "/x//y").as_uri() == "http://h/x//y"
    assert (base / "/").path == "/"
    assert (base / "/x/").path == "/x/"


def test_a_trailing_slash_is_still_kept_and_two_are_two():
    base = Uri("http://h/d")
    assert (base / "x/").path == "/d/x/"
    assert (base / "x//").path == "/d/x//"
    assert (base / "x").path == "/d/x"


def test_a_single_slash_between_names_is_unchanged():
    base = Uri("http://h/d")
    assert (base / "x/y/z").path == "/d/x/y/z"
    assert (base / "x" / "y").path == "/d/x/y"
    assert base.joinpath("x", "y/z").path == "/d/x/y/z"


def test_dot_segments_and_empty_segments_in_one_key():
    base = Uri("http://h/d")
    for key in ("x//../y", "x/./y//z", "x//y/..", "../x//y"):
        assert (base / key).as_uri() == Uri("http://h/d", key).as_uri(), key
    assert (base / "x//../y").path == "/d/x/y"
    assert (base / "x/./y//z").path == "/d/x/y//z"


def test_an_empty_key_and_an_empty_argument_change_nothing():
    base = Uri("http://h/d")
    assert (base / "").as_uri() == "http://h/d"
    assert base.joinpath("", "x", "").as_uri() == "http://h/d/x"


def test_a_str_destination_keeps_its_empty_segment_like_a_join(tmp_path):
    path = UriPath((tmp_path / "f").as_uri())
    target = path._rename_target("a//b")
    assert target.path.endswith("/a//b")
    assert target.as_uri() == (path.parent / "a//b").as_uri()
