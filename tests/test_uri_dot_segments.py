"""A URI path means what it spells: RFC 3986 5.2.4 removes its dot segments
whether it was written in the constructor, joined as a `str` or joined as a
`Uri`. The join itself stays pathlib-style concatenation (`/d` + `x` is
`/d/x`); only the removal of `.` and `..` is the RFC's.
"""

import os

import pytest

from pathlib_next.uri import Uri, UriPath

WINDOWS = os.name == "nt"

#: `base` + `key` joined, then RFC 3986 5.2.4 over the whole path. A base
#: ending in a slash is the same directory as one without it.
_ANSWERS = {
    "..": "dotseg://h/",
    "../x": "dotseg://h/x",
    "x/..": "dotseg://h/d/",
    "x/../y": "dotseg://h/d/y",
    "./x": "dotseg://h/d/x",
    "../../x": "dotseg://h/x",
    "x/./": "dotseg://h/d/x/",
    "%2e%2e/x": "dotseg://h/x",
}
_BASES = ["dotseg://h/d", "dotseg://h/d/"]
_CLASSES = [Uri, UriPath]


def _spellings(cls, base, key):
    """The three ways to say `key` under `base`."""
    return {
        "constructor": cls(base, key),
        "str join": cls(base) / key,
        "Uri join": cls(base) / Uri(key),
    }


@pytest.mark.parametrize("cls", _CLASSES)
@pytest.mark.parametrize("base", _BASES)
@pytest.mark.parametrize("key", list(_ANSWERS))
def test_every_spelling_gives_the_answer_of_the_joined_path(cls, base, key):
    results = _spellings(cls, base, key)
    if key.startswith("%2e"):
        # A `str` join key is an already-decoded path: `%2e%2e` is the
        # literal name of a directory there, not a dot segment.
        assert str(results.pop("str join")).endswith("/d/%252e%252e/x")
    assert {name: str(path) for name, path in results.items()} == {
        name: _ANSWERS[key] for name in results
    }
    for path in results.values():
        assert cls(path.as_uri()) == path


@pytest.mark.parametrize("cls", _CLASSES)
def test_a_str_join_and_the_constructor_agree_on_a_file_name_below_the_base(cls):
    base = cls("dotseg://h/d/x")
    assert str(base / "..") == str(cls("dotseg://h/d/x/..")) == "dotseg://h/d/"
    assert str(base / ".." / "y") == "dotseg://h/d/y"
    assert str(base.joinpath("..", "y")) == "dotseg://h/d/y"


def test_a_name_that_only_looks_like_a_dot_segment_is_a_name():
    base = Uri("dotseg://h/d/")
    for name in ("...", ".hidden", "a..b", "..x", "x."):
        assert (base / name).path == f"/d/{name}"
        assert Uri(base, name).path == f"/d/{name}"
        assert base._make_child_relpath(name).path == f"/d/{name}"
        assert Uri((base / name).as_uri()) == base / name


@pytest.mark.parametrize("base", _BASES)
def test_a_child_built_from_a_listed_name_is_not_resolved(base):
    """The listing route builds a child by name and never resolves one; a
    name that is not one plain component is refused before this point."""
    parent = UriPath(base)
    assert parent._make_child_relpath("x").path == "/d/x"
    assert parent._make_child_relpath("x.y").path == "/d/x.y"

    class Listed(UriPath):
        __SCHEMES = ("dot-listing",)
        __slots__ = ()

        def _listdir(self):
            return ["a", "..", ".", "b/c", "", "d"]

        def stat(self, *, follow_symlinks=True):
            raise FileNotFoundError(self.path)

    listed = Listed("dot-listing://h/d/")
    assert [p.path for p in listed.iterdir()] == ["/d/a", "/d/d"]


# --- percent-encoded dot segments ------------------------------------------


@pytest.mark.parametrize(
    "spelled",
    [
        "dotseg://h/safe/%2e%2e/secret",
        "dotseg://h/safe/%2E%2E/secret",
        "dotseg://h/safe/.%2e/secret",
        "dotseg://h/safe/%2e./secret",
        "dotseg://h/safe/..%2Fsecret",
        "dotseg://h/safe/%2e%2e%2fsecret",
    ],
)
def test_a_percent_encoded_dot_segment_is_removed_after_decoding(spelled):
    for cls in _CLASSES:
        path = cls(spelled)
        assert path.path == "/secret"
        assert path.segments == ("", "secret")
        assert str(path) == "dotseg://h/secret"
        assert cls(path.as_uri()) == path
        assert path.parent.path == "/"


def test_a_percent_encoded_dot_is_removed_too():
    assert Uri("dotseg://h/a/%2e/b").path == "/a/b"
    assert Uri("dotseg://h/a/%2e").path == "/a/"


def test_an_escape_that_is_not_a_dot_segment_is_left_alone():
    path = Uri("dotseg://h/a%20b/c%2Ed.txt")
    assert path.path == "/a b/c.d.txt"
    assert Uri(path.as_uri()) == path


def test_a_data_payload_is_never_resolved():
    assert Uri("data:,a/%2e%2e/b").path == ",a/../b"


# --- a reference past its start ----------------------------------------------


def test_a_relative_reference_keeps_the_dots_it_cannot_resolve():
    """The constructor keeps a leading `..`, and so does a join: one routine
    removes the dot segments, so `Uri("a") / "../../x"` is what
    `Uri("a/../../x")` is."""
    assert Uri("a/../../x").path == "../x"
    assert (Uri("a") / "../../x").path == "../x"
    assert Uri("a", "../../x").path == "../x"
    assert (Uri("a") / Uri("../../x")).path == "../x"
    assert Uri("..").path == "../"
    assert (Uri("") / "..").path == "../"
    assert (Uri("a/b") / "../..").path == Uri("a/b/../..").path == "./"


@pytest.mark.parametrize("cls", _CLASSES)
def test_a_dot_segment_never_climbs_past_the_root_of_an_absolute_path(cls):
    assert str(cls("dotseg://h/../x")) == "dotseg://h/x"
    assert str(cls("dotseg://h/") / "..") == "dotseg://h/"
    assert str(cls("dotseg://h") / "..") == "dotseg://h/"
    assert str(cls("dotseg://h/d") / "../../..") == "dotseg://h/"
    assert cls("/a") / ".." / ".." == cls("/")


def test_a_join_below_the_root_reaches_the_same_path_as_its_spelling():
    for spelling, joined in (
        ("dotseg://h/d/../x", Uri("dotseg://h/d/") / "../x"),
        ("dotseg://h/d/x/../..", Uri("dotseg://h/d/x") / "../.."),
        ("dotseg://h/d/./x/", Uri("dotseg://h/d") / "./x/"),
    ):
        assert Uri(spelling) == joined


def test_a_destination_with_dots_resolves_like_a_join():
    source = UriPath("sftp://user@host/mnt/sub/a.txt")
    for target, path in (
        ("../b.txt", "/mnt/b.txt"),
        ("./b.txt", "/mnt/sub/b.txt"),
        ("../../b.txt", "/b.txt"),
        ("x/../y", "/mnt/sub/y"),
    ):
        assert source._coerce_target(target).path == path
        assert source._rename_target(target).path == path


# --- the drive of a file: path on Windows -------------------------------------


@pytest.mark.skipif(not WINDOWS, reason="a drive letter is only an anchor on Windows")
@pytest.mark.parametrize("cls", [Uri, UriPath])
def test_dot_segments_never_climb_above_a_windows_drive(cls):
    assert cls("file:///D:/data/../../x").path.removeprefix("/") == "D:/x"
    assert cls("file:///C:/d/../../../x").as_uri() == "file:/C:/x"
    assert cls("file:///C:/d/..").path.removeprefix("/") == "C:/"
    assert cls("file:///C:/d/%2e%2e/%2e%2e/x").path.removeprefix("/") == "C:/x"


@pytest.mark.skipif(not WINDOWS, reason="a drive letter is only an anchor on Windows")
def test_a_join_never_climbs_above_a_windows_drive():
    base = UriPath("file:///C:/d/")
    assert (base / "../../../x").path == "C:/x"
    assert (base / "..").path == "C:/"
    assert (base / "../..").path == "C:/"
    assert (base / "../../..").as_uri() == "file:/C:/"
    assert UriPath("file:///C:/d", "../../../x").path == "C:/x"
    assert (base / "D:/e/../../f").path == "D:/f"


@pytest.mark.skipif(WINDOWS, reason="a drive letter is an ordinary name off Windows")
def test_a_drive_shaped_name_is_an_ordinary_directory_off_windows():
    assert UriPath("file:///D:/data/../../x").path == "/x"
    assert (UriPath("file:///C:/d/") / "../../../x").path == "/x"
    assert (UriPath("file:///C:/d/") / "..").path == "/C:/"
