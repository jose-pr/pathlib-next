"""The value behaviour of `Uri` and `Source`: the types of `query` and
`fragment`, printing, construction errors, tuple access, caches that survive
a second initialization, `with_path()`/`with_segments()` and the `data:`
payload."""

from __future__ import annotations

import pathlib
import threading

import pytest

pytest.importorskip("uritools")

from pathlib_next.uri import Uri, UriPath
from pathlib_next.uri.query import Query
from pathlib_next.uri.source import Source

# --- query and fragment are always a Query and a str -------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda: Uri("http://h/x"),
        lambda: Uri("http://h/x") / "y",
        lambda: Uri("http://h/x").parent,
        lambda: UriPath("http://h/x"),
        lambda: Uri("http://h/x").with_name("z"),
        lambda: Uri("a/b"),
        lambda: Uri(),
        lambda: Uri("http://h/x").with_path("/q"),
        lambda: "pre" / Uri("x"),
    ],
)
def test_a_uri_without_a_query_or_fragment_has_an_empty_one(build):
    uri = build()
    assert isinstance(uri.query, Query)
    assert uri.query == ""
    assert uri.query.to_dict() == {}
    assert list(uri.query) == []
    assert uri.fragment == ""
    assert uri.parts[2] == "" and uri.parts[3] == ""


def test_a_query_keeps_its_type_and_text_through_every_derivation():
    uri = Uri("http://h/x?a=1%2B2&b=%26#frag")
    assert isinstance(uri.query, Query)
    assert str(uri.query) == "a=1%2B2&b=%26"
    assert uri.fragment == "frag"
    assert uri.query.to_dict() == {"a": ["1+2"], "b": ["&"]}
    assert uri.with_name("y").query == uri.query
    assert str(uri.with_query("c=3").query) == "c=3"
    assert str(uri.with_query({"k": "a+b"}).query) == "k=a%2Bb"
    assert uri.with_fragment("").fragment == ""


# --- repr -------------------------------------------------------------------------


def test_repr_of_a_uri_that_was_never_read_shows_the_uri():
    assert repr(Uri("http://h/x")) == "Uri('http://h/x')"
    assert repr(UriPath("http://u:pw@h/x")) == "HttpPath('http://u@h/x')"
    assert repr(Uri()) == "Uri('')"
    joined = Uri("http://h/d", "../x")
    assert repr(joined) == "Uri('http://h/x')"


def test_repr_of_a_uri_that_does_not_parse_still_prints():
    broken = Uri("http://[::1/x")
    assert "Uri" in repr(broken)
    with pytest.raises(ValueError):
        broken.source


# --- construction errors ----------------------------------------------------------------


def test_none_and_empty_text_are_the_empty_uri():
    for empty in (None, "", b""):
        assert Uri(empty).as_uri() == ""
        assert Uri("http://h/x", empty).as_uri() == "http://h/x"


@pytest.mark.parametrize("bad", [0, 5, 1.5, [], (), object(), True])
def test_an_argument_of_the_wrong_type_names_its_own_type(bad):
    with pytest.raises(TypeError) as caught:
        Uri(bad)
    assert type(bad).__name__ in str(caught.value)
    assert "NoneType" not in str(caught.value)


# --- Source as a tuple -------------------------------------------------------------------


def test_source_slices_like_the_tuple_it_is():
    source = Source("http", "u:p", "h", 80)
    assert source[0:2] == ("http", "u:p")
    assert source[::-1] == (80, "h", "u:p", "http")
    assert source[-1] == 80
    assert source[1:] == ("u:p", "h", 80)
    assert source["host"] == "h"
    assert source[2] == "h"
    with pytest.raises(IndexError):
        source[7]
    assert dict(source) == {
        "scheme": "http",
        "userinfo": "u:p",
        "host": "h",
        "port": 80,
    }
    assert tuple(source) == ("http", "u:p", "h", 80)


@pytest.mark.parametrize(
    "text", ["file:", "x:", "http://h", "//h", "http://u:p@h:1", "file://localhost"]
)
def test_from_str_reads_an_authority_as_a_uri_does(text):
    assert Source.from_str(text) == Uri(text).source


# --- the caches -------------------------------------------------------------------------------


def test_initializing_again_with_the_same_parts_keeps_the_caches():
    uri = Uri("http://h/a/b.c")
    assert uri.segments == ("", "a", "b.c")
    assert (uri.suffix, uri.stem) == (".c", "b")
    cached = (uri._segments_cache, uri._suffix_cache, uri._stem_cache)
    uri._init(uri.source, uri.path, uri.query, uri.fragment)
    assert (uri._segments_cache, uri._suffix_cache, uri._stem_cache) == cached
    assert uri._segments_cache is cached[0]


def test_initializing_again_with_other_parts_resets_every_cache():
    uri = Uri("http://h/a/b.c")
    uri.as_uri()
    uri.normalized_path
    uri.segments, uri.suffix, uri.stem
    uri._init(uri.source, "/d/e.f", uri.query, uri.fragment)
    assert uri.as_uri() == "http://h/d/e.f"
    assert uri.normalized_path == "/d/e.f"
    assert uri.segments == ("", "d", "e.f")
    assert (uri.suffix, uri.stem) == (".f", "e")


def test_a_cached_property_returns_what_it_computed_not_the_slot(monkeypatch):
    # Another thread's _init can reset the slot between the store and the
    # read; the property must still answer with the value it computed.
    for attribute, slot in (
        ("segments", "_segments_cache"),
        ("suffix", "_suffix_cache"),
        ("stem", "_stem_cache"),
    ):
        fresh = Uri("http://h/a/b.c")
        fresh.path
        original = type(fresh).__setattr__

        def reset(self, name, value, slot=slot, original=original):
            original(self, name, value)
            if name == slot:
                original(self, name, None)

        monkeypatch.setattr(type(fresh), "__setattr__", reset, raising=False)
        try:
            assert getattr(fresh, attribute) is not None
        finally:
            monkeypatch.undo()


def test_threads_that_parse_the_same_uri_lazily_agree():
    errors = []

    def read(uri, barrier):
        barrier.wait()
        try:
            assert uri.segments == ("", "a", "b.c")
            assert (uri.suffix, uri.stem, uri.name) == (".c", "b", "b.c")
        except Exception as error:  # pragma: no cover - only on a failure
            errors.append(error)

    for _ in range(50):
        uri = Uri("http://h/a/b.c")
        barrier = threading.Barrier(4)
        threads = [threading.Thread(target=read, args=(uri, barrier)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert errors == []


@pytest.mark.parametrize(
    "name", ["a.", "a.b", ".a", "a.b.", "..a", "a..b", "a.b.c", "...", "a.b..c"]
)
def test_suffix_and_stem_are_those_of_the_running_pathlib(name):
    uri = Uri("http://h/d/" + name)
    expected = pathlib.PurePosixPath("d") / name
    assert expected.name == name
    assert (uri.suffix, uri.stem) == (expected.suffix, expected.stem)


# --- with_path and with_segments --------------------------------------------------------------


def test_with_path_gives_a_relative_path_under_an_authority_its_slash():
    uri = Uri("http://h/x")
    for derived in (
        uri.with_path("y"),
        uri.with_segments("y"),
        uri.with_segments("y", "z"),
    ):
        assert derived.path.startswith("/")
        assert str(derived) == repr(derived)[len("Uri('") : -2]
        hash(derived)
        assert derived == Uri(derived.as_uri())
    assert uri.with_path("y").as_uri() == "http://h/y"
    assert uri.with_segments("y", "z").as_uri() == "http://h/y/z"
    assert uri.with_path("").as_uri() == "http://h"
    assert Uri("a/b").with_path("c").as_uri() == "c"
    assert Uri("file:///x").with_path("y").as_uri() == "file:y"
    assert UriPath("sftp://h/x").with_path("y").as_uri() == "sftp://h/y"


def test_with_path_accepts_a_pure_path_object():
    uri = Uri("http://h/x")
    assert uri.with_path(Uri("a/b")).as_uri() == "http://h/a/b"
    assert uri.with_path(pathlib.PurePosixPath("a/b")).as_uri() == "http://h/a/b"


def test_with_segments_keeps_the_root_marker_of_the_segments_spelling():
    uri = Uri("http://h/x/y")
    assert uri.with_segments("", "a", "b").as_uri() == "http://h/a/b"
    assert uri.with_segments("", "").as_uri() == "http://h/"
    assert uri.with_segments().as_uri() == "http://h"
    assert uri.with_name("z").as_uri() == "http://h/x/z"
    assert uri.parent.as_uri() == "http://h/x"


@pytest.mark.parametrize(
    "segment",
    [
        pathlib.PurePosixPath("etc/hosts"),
        pathlib.PureWindowsPath("etc\\hosts"),
        pathlib.Path("etc") / "hosts",
        b"etc/hosts",
    ],
)
def test_with_segments_accepts_what_the_constructor_accepts(segment):
    uri = Uri("http://h/x")
    assert uri.with_segments(segment).as_uri() == "http://h/etc/hosts"


def test_with_segments_refuses_what_is_not_a_path():
    with pytest.raises(TypeError):
        Uri("http://h/x").with_segments(5)


def test_samefile_accepts_an_os_pathlike(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    path = UriPath((tmp_path / "f.txt").as_uri())
    assert path.samefile(tmp_path / "f.txt") is True
    assert path.samefile(str(tmp_path / "f.txt")) is True
    (tmp_path / "g.txt").write_text("y")
    assert path.samefile(tmp_path / "g.txt") is False


# --- one join code ---------------------------------------------------------------------------


def test_an_error_inside_the_construction_of_a_join_is_not_an_unsupported_operand():
    class Boom(Uri):
        __slots__ = ()

        def _join_object(self, *parts):
            raise TypeError("a bug in construction")

    with pytest.raises(TypeError, match="a bug in construction"):
        Boom("x") / Uri("y")
    with pytest.raises(TypeError, match="a bug in construction"):
        Boom("x").joinpath(Uri("y"))


def test_what_cannot_be_joined_is_not_implemented_for_both_classes():
    for cls in (Uri, UriPath):
        with pytest.raises(TypeError, match="unsupported operand"):
            cls("unk://h/x") / 5
        with pytest.raises(TypeError, match="int"):
            cls("unk://h/x").joinpath(5)
        with pytest.raises(TypeError, match="unsupported operand"):
            5 / cls("unk://h/x")


def test_the_prefix_form_joins_a_decoded_path_in_front_for_both_classes():
    assert ("pre" / Uri("x")).as_uri() == "pre/x"
    assert ("C:/pre" / Uri("x")).as_uri() == "./C:/pre/x"
    assert "pre" / UriPath("http://h/x") == UriPath("http://h/x")
    assert type("pre" / UriPath("http://h/x")).__name__ == "HttpPath"
