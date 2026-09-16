"""Pure-path parity of the generic `Pathname` code (`MemPath`, `Uri`) and of
the fspath version shims, checked against `pathlib.PurePosixPath` /
`PureWindowsPath` on the running interpreter.

Covers the 2026-09-15 review's pure-path findings: right-anchored
`match()`, `parents`/`parent` keeping the root, `with_name()` validation,
`MemPath` normalization, and authority roots in `relative_to()`.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

from pathlib_next.fspath import LocalPath, PosixPathname, WindowsPathname
from pathlib_next.mempath import MemPath, MemPathBackend
from pathlib_next.uri import Uri

GENERIC = [MemPath, Uri]

# --- match(): right-anchored, per segment ---------------------------------

MATCH_PATHS = [
    "a/b/c.py",
    "/a/b/c.py",
    "/",
    "a",
    "/a",
    "a//b/./c",
    "x/y/setup.py",
    "/data/2024/x.csv",
]
MATCH_PATTERNS = [
    "*",
    "*.py",
    "b/*.py",
    "a/*.py",
    "/a/b/*.py",
    "/*",
    "*/a",
    "*/*",
    "/",
    "**",
    "**/c.py",
    "a/b/c.py",
    "*/b/*",
    "setup.py",
    "2024/*.csv",
    "b/",
    "./c",
    "?",
    "[ab]/*",
]


def _oracle_match(path, pattern, **kwargs):
    try:
        return pathlib.PurePosixPath(path).match(pattern, **kwargs)
    except ValueError:
        return ValueError


def _our_match(p, pattern, **kwargs):
    try:
        return p.match(pattern, **kwargs)
    except ValueError:
        return ValueError


@pytest.mark.parametrize("cls", GENERIC)
@pytest.mark.parametrize("path", MATCH_PATHS)
@pytest.mark.parametrize("pattern", MATCH_PATTERNS)
def test_generic_match_matches_pure_posix_path(cls, path, pattern):
    assert _our_match(cls(path), pattern) == _oracle_match(path, pattern)


@pytest.mark.parametrize("cls", GENERIC)
@pytest.mark.parametrize("pattern", ["", "."])
def test_generic_match_empty_pattern_raises(cls, pattern):
    with pytest.raises(ValueError):
        cls("a/b").match(pattern)


@pytest.mark.parametrize(
    "uri,pattern,expected",
    [
        ("http://h/a/b/c.py", "b/*.py", True),
        ("http://h/a/b/c.py", "/a/b/*.py", True),
        ("http://h/a/b/c.py", "/b/*.py", False),
        ("sftp://user@host/dir/x.py", "dir/*.py", True),
        ("sftp://user@host/dir/x.py", "/dir/*.py", True),
        ("sftp://user@host/dir/x.py", "x.py", True),
        ("s3://bucket/data/x.csv", "data/*.csv", True),
        # The host is never part of what is matched.
        ("sftp://host/dir/x.py", "host*", False),
        ("sftp://host/dir/x.py", "*/*/*/*.py", False),
        # An authority with an empty path is its root.
        ("http://h", "/", True),
        ("http://h/", "/", True),
    ],
)
def test_uri_match_uses_path_segments_not_host(uri, pattern, expected):
    assert Uri(uri).match(pattern) is expected


def test_uri_match_and_full_match_agree_on_absolute_patterns():
    u = Uri("http://h/a/b.py")
    assert u.match("/a/*.py") is True
    assert u.full_match("/a/*.py") is True


@pytest.mark.parametrize("cls", GENERIC)
def test_generic_match_case_sensitive_keyword(cls):
    p = cls("a/B.PY")
    assert p.match("*.py") is False
    assert p.match("*.py", case_sensitive=False) is True
    assert p.match("b.py", case_sensitive=False) is True
    assert p.match("*.PY", case_sensitive=True) is True


@pytest.mark.parametrize("cls", GENERIC)
def test_generic_match_accepts_compiled_regex_on_the_path_string(cls):
    import re

    assert cls("/a/b.py").match(re.compile(r"/a/.*\.py"))
    assert not cls("/a/b.py").match(re.compile(r"b\.py"))
    assert Uri("http://h/a/b.py").match(re.compile(r"/a/b\.py"))


# --- fspath shims: match(case_sensitive=) and full_match() before 3.12/3.13 --


# Recorded from CPython 3.14's match(case_sensitive=); rows whose answer
# depends on how the running stdlib treats the anchor are left out.
MATCH_CASE_TABLE = [
    ("P", "a/B.py", "*.py", True, True),
    ("P", "a/B.py", "*.py", False, True),
    ("P", "a/b.py", "*.PY", True, False),
    ("P", "a/b.py", "*.PY", False, True),
    ("P", "A/b", "a/B", True, False),
    ("P", "A/b", "a/B", False, True),
    ("P", "/a/b", "/A/*", True, False),
    ("P", "/a/b", "/A/*", False, True),
    ("P", "C:/a/b", "c:/a/*", True, False),
    ("P", "C:/a/b", "c:/a/*", False, True),
    ("P", "a/b", "a/b", True, True),
    ("P", "//s/sh/a", "//S/SH/a", True, False),
    ("P", "//s/sh/a", "//S/SH/a", False, True),
    ("W", "a/B.py", "*.py", True, True),
    ("W", "a/b.py", "*.PY", True, False),
    ("W", "a/b.py", "*.PY", False, True),
    ("W", "A/b", "a/B", True, False),
    ("W", "A/b", "a/B", False, True),
    ("W", "/a/b", "/A/*", True, False),
    ("W", "/a/b", "/A/*", False, True),
    ("W", "C:/a/b", "c:/a/*", True, False),
    ("W", "C:/a/b", "c:/a/*", False, True),
    ("W", "a/b", "a/b", False, True),
    ("W", "//s/sh/a", "//S/SH/a", True, False),
    ("W", "//s/sh/a", "//S/SH/a", False, True),
]


@pytest.mark.parametrize(
    "flavour,path,pattern,case_sensitive,expected", MATCH_CASE_TABLE
)
def test_fs_match_case_sensitive_keyword_on_every_version(
    flavour, path, pattern, case_sensitive, expected
):
    classes = [WindowsPathname] if flavour == "W" else [PosixPathname]
    if (flavour == "W") == (sys.platform == "win32"):
        classes.append(LocalPath)
    for cls in classes:
        assert cls(path).match(pattern, case_sensitive=case_sensitive) is expected


@pytest.mark.parametrize("cls", [PosixPathname, WindowsPathname, LocalPath])
@pytest.mark.parametrize(
    "path,pattern", [("a/B.py", "*.py"), ("/a", "*/a"), ("C:/a/b", "/a/b")]
)
def test_fs_match_without_keyword_is_stdlib(cls, path, pattern):
    flavour = (
        pathlib.PureWindowsPath
        if cls is WindowsPathname or (cls is LocalPath and sys.platform == "win32")
        else pathlib.PurePosixPath
    )
    assert cls(path).match(pattern) == flavour(path).match(pattern)


def test_fs_match_default_still_resolves_to_stdlib():
    # The shim only exists where stdlib lacks case_sensitive=; elsewhere the
    # stdlib method itself must resolve (MRO guard).
    for cls in (PosixPathname, WindowsPathname, LocalPath):
        if sys.version_info >= (3, 12):
            assert cls.match is pathlib.PurePath.match
        if sys.version_info >= (3, 13):
            assert cls.full_match is pathlib.PurePath.full_match


# Recorded from CPython 3.14's PurePath.full_match; asserted on every version
# (on 3.13+ this checks stdlib itself, below that the shim).
FULL_MATCH_TABLE = [
    ("P", "/a/b", "/a/*", True),
    ("W", "/a/b", "/a/*", True),
    ("P", "/a/b", "/**", True),
    ("W", "/a/b", "/**", True),
    ("P", "/a/b", "**", True),
    ("P", "/a/b", "**/b", True),
    ("W", "/a/b", "/**/b", True),
    ("P", "/a/b", "*/a/b", False),
    ("P", "/", "/", True),
    ("P", "/", "**", True),
    ("P", "/", "*", False),
    ("P", "", "**", True),
    ("P", "", "*", False),
    ("P", "", "", True),
    ("P", "a", "", False),
    ("P", "a/b", "a/**", True),
    ("P", "a", "a/**", False),
    ("P", "a/b/c", "a/**/c", True),
    ("P", "a/c", "a/**/c", True),
    ("P", "a/b", "a/*/", True),
    ("P", "a/b", "A/B", False),
    ("W", "a/b", "A/B", True),
    ("P", "a/b", "./a/b", True),
    ("P", "a/b", "a**", False),
    ("P", "ab/c", "a**/c", True),
    ("W", "C:/a/b", "C:/a/*", True),
    ("W", "C:/a/b", "c:/a/*", True),
    ("W", "C:/a/b", "**/b", True),
    ("W", "C:/a/b", "/a/b", False),
    ("W", "C:/a/b", "C:**", False),
    ("P", "a/b", "a\\*", False),
    ("W", "a/b", "a\\*", True),
    ("W", "//s/sh/a", "//S/sh/*", True),
    ("W", "//s/sh/a", "**", True),
    ("W", "C:a", "C:*", True),
    ("W", "C:/", "C:/", True),
    ("W", "C:/a", "*/a", True),
    ("P", ".a/b", "*/b", True),
    ("P", "a/.b", "a/*", True),
    ("P", "a/b", "a/[b]", True),
    ("P", "a/b", "a/[!b]", False),
    ("P", "a/b", "a/?", True),
]


@pytest.mark.parametrize("flavour,path,pattern,expected", FULL_MATCH_TABLE)
def test_fs_full_match_matches_cpython_314(flavour, path, pattern, expected):
    cls = WindowsPathname if flavour == "W" else PosixPathname
    assert cls(path).full_match(pattern) is expected


def test_localpath_full_match_rooted_and_case_keyword():
    anchor = "C:/" if sys.platform == "win32" else "/"
    assert LocalPath(anchor + "a/b").full_match(anchor + "a/*")
    assert LocalPath(anchor + "a/b").full_match(anchor + "**")
    assert LocalPath("a/B.py").match("*.py", case_sensitive=False)


# --- parents / parent keep the root ----------------------------------------


@pytest.mark.parametrize("cls", GENERIC)
@pytest.mark.parametrize("path", ["/a/b", "/a/b/c.py", "/a", "/", "a/b/c", "a", ""])
def test_generic_parents_match_pure_posix_path(cls, path):
    theirs = pathlib.PurePosixPath(path)
    ours = cls(path)

    def spell(p):
        s = p.as_posix()
        return "" if s == "." else s

    assert [spell(p) for p in ours.parents] == [spell(p) for p in theirs.parents]
    assert len(ours.parents) == len(theirs.parents)
    assert spell(ours.parent) == spell(theirs.parent)


def test_mempath_root_parents_and_is_relative_to():
    assert [str(p) for p in MemPath("/a/b").parents] == ["/a", "/"]
    assert MemPath("/a/b").is_relative_to("/")
    assert MemPath("/a/b").is_relative_to(MemPath("/"))
    assert MemPath("/").parent == MemPath("/")
    assert not MemPath("/a").is_relative_to("")


def test_uri_parents_reach_the_authority_root_once():
    parents = [str(p) for p in Uri("http://h/a/b/c.py").parents]
    assert parents == ["http://h/a/b", "http://h/a", "http://h/"]
    assert Uri("http://h/a").parent == Uri("http://h/")
    assert Uri("/c").parent.path == "/"
    assert Uri("/c").parent.root == "/"
    # The relative case is unchanged (documented divergence).
    assert Uri("a").parent.path == ""


def test_parents_negative_index_and_slice_keep_the_root():
    for p in (MemPath("/a/b/c"), Uri("/a/b/c")):
        assert p.parents[-1].as_posix() == "/"
        assert [x.as_posix() for x in p.parents[1:]] == ["/a", "/"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file: root")
def test_fileuri_top_level_parent_is_root_not_cwd():
    from pathlib_next.uri import UriPath

    parent = UriPath("file:///Users").parent
    assert parent.path == "/"
    assert str(parent.filepath) == "/"


# --- with_name / with_stem / with_suffix validation --------------------------

BAD_NAMES = ["", ".", "x/y", "../../etc/passwd", "/abs"]


@pytest.mark.parametrize(
    "path", ["http://h/d/f.txt", "sftp://h/srv/uploads/report.txt", "d/f.txt"]
)
@pytest.mark.parametrize("name", BAD_NAMES)
def test_uri_with_name_rejects_traversal_and_separators(path, name):
    with pytest.raises(ValueError):
        Uri(path).with_name(name)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_mempath_with_name_rejects_traversal_and_separators(name):
    with pytest.raises(ValueError):
        MemPath("/srv/up.txt").with_name(name)


@pytest.mark.parametrize("cls", GENERIC)
@pytest.mark.parametrize("name", ["", ".", "x/y"])
def test_generic_with_name_rejects_what_pathlib_rejects(cls, name):
    with pytest.raises(ValueError):
        pathlib.PurePosixPath("d/f.txt").with_name(name)
    with pytest.raises(ValueError):
        cls("d/f.txt").with_name(name)


@pytest.mark.parametrize("cls", GENERIC)
def test_generic_with_stem_and_with_suffix_validate_the_result(cls):
    p = cls("d/f.txt")
    with pytest.raises(ValueError):
        p.with_suffix(".a/b")
    with pytest.raises(ValueError):
        p.with_stem("x/y")
    with pytest.raises(ValueError):
        p.with_stem("../..")
    with pytest.raises(ValueError):
        cls("d/f").with_stem("")
    # Legitimate names still work, including URI-significant characters in
    # a decoded name.
    assert p.with_name("a?b#c%20.txt").name == "a?b#c%20.txt"
    assert p.with_name("..x").name == "..x"
    assert p.with_stem("g").name == "g.txt"
    assert p.with_suffix(".py").name == "f.py"


@pytest.mark.parametrize("path", ["http://h/d/f.txt", "d/f.txt"])
def test_with_name_accepts_dotdot_like_pathlib(path):
    assert pathlib.PurePosixPath("d/f.txt").with_name("..").name == ".."
    assert Uri(path).with_name("..").segments[-1] == ".."
    assert MemPath("/srv/up.txt").with_name("..").as_posix() == "/srv/.."


def test_fs_with_name_keeps_stdlib_semantics():
    # PosixPathname/LocalPath resolve stdlib's with_name, which accepts "..".
    assert PosixPathname("d/f").with_name("..").as_posix() == "d/.."


# --- MemPath normalization -----------------------------------------------

MEM_PATHS = [
    "",
    "/",
    "a",
    "/a",
    "a/",
    "/a/",
    "a//b",
    "a/./b",
    "./a",
    "/./a/.",
    "a/../b",
    "///a",
]


@pytest.mark.parametrize("path", MEM_PATHS)
def test_mempath_str_matches_pure_posix_path(path):
    theirs = str(pathlib.PurePosixPath(path))
    assert str(MemPath(path)) == ("" if theirs == "." else theirs)
    p = MemPath(path)
    assert p.name == pathlib.PurePosixPath(path).name
    assert p.root == pathlib.PurePosixPath(path).root


@pytest.mark.parametrize(
    "args",
    [
        ("/", "a"),
        ("/root", "/etc"),
        ("a", "/b"),
        ("a", "", "b"),
        ("a/", "b/"),
        ("", "a"),
        ("/a", ".", "b"),
    ],
)
def test_mempath_join_matches_pure_posix_path(args):
    theirs = str(pathlib.PurePosixPath(*args))
    assert str(MemPath(*args)) == ("" if theirs == "." else theirs)


def test_mempath_equality_and_hash_follow_normalization():
    backend = MemPathBackend()
    root = MemPath("/", backend=backend)
    child = root / "a.txt"
    assert str(child) == "/a.txt"
    assert child == MemPath("/a.txt", backend=backend)
    assert hash(child) == hash(MemPath("/a.txt"))
    assert MemPath("a/") == MemPath("a")
    assert MemPath("a//b") == MemPath("a/./b") == MemPath("a/b")
    assert (root / "/etc") == MemPath("/etc")
    assert MemPath("d/").name == "d"
    assert MemPath("d/").with_suffix(".x") == MemPath("d.x")
    assert child.as_uri() == "mempath:/a.txt"


def test_mempath_iterdir_children_equal_constructed_paths():
    backend = MemPathBackend()
    root = MemPath("/", backend=backend)
    (root / "sub").mkdir()
    (root / "f.txt").write_text("x")
    children = set(root.iterdir())
    assert children == {MemPath("/sub"), MemPath("/f.txt")}
    assert all(c.backend is backend for c in children)
    assert list((root / "sub").iterdir()) == []
    # Relative roots list relative children.
    rel = MemPath("", backend=backend)
    assert {str(c) for c in rel.iterdir()} == {"sub", "f.txt"}


def test_mempath_io_still_addresses_the_same_entries():
    backend = MemPathBackend()
    MemPath("/d/", backend=backend).mkdir()
    MemPath("d//f.txt", backend=backend).write_text("hi")
    assert MemPath("/d/./f.txt", backend=backend).read_text() == "hi"
    assert MemPath("/", backend=backend).is_dir()
    assert MemPath("", backend=backend).is_dir()


def test_mempath_with_segments_keeps_backend_and_root():
    backend = MemPathBackend()
    p = MemPath("/a/b", backend=backend)
    assert p.with_segments("", "x") == MemPath("/x")
    assert p.with_segments("", "") == MemPath("/")
    assert p.with_segments("x").backend is backend
    assert p.with_segments() == MemPath("")


# --- relative_to / is_relative_to with authority roots ----------------------


@pytest.mark.parametrize(
    "path,other,expected",
    [
        ("s3://bucket/dir/key", "s3://bucket", "dir/key"),
        ("s3://bucket/dir/key", "s3://bucket/", "dir/key"),
        ("http://h/a", "http://h", "a"),
        ("/a/b", "/", "a/b"),
        ("a/b", "", "a/b"),
    ],
)
def test_uri_relative_to_authority_root(path, other, expected):
    u = Uri(path)
    assert u.is_relative_to(other) is True
    assert u.relative_to(other).as_posix() == expected


def test_uri_relative_to_own_parent():
    for u in (Uri("http://h/a"), Uri("/a"), Uri("s3://b/k")):
        assert u.relative_to(u.parent).as_posix() == u.name
        assert u.is_relative_to(u.parent)


@pytest.mark.parametrize(
    "path,other,expected",
    [
        ("/a/b", "/c", "../a/b"),
        ("http://h/a/b", "http://h/c", "../a/b"),
        ("/x/a/b", "/x/c", "../a/b"),
        ("c", "a/b", "../../c"),
    ],
)
def test_uri_relative_to_walk_up_to_the_root(path, other, expected):
    got = Uri(path).relative_to(other, walk_up=True).as_posix()
    assert got == expected
    if sys.version_info >= (3, 12):
        oracle = pathlib.PurePosixPath(Uri(path).path).relative_to(
            Uri(other).path, walk_up=True
        )
        assert got == oracle.as_posix()


def test_uri_absolute_is_not_relative_to_the_empty_path():
    assert Uri("/a").is_relative_to("") is False
    assert pathlib.PurePosixPath("/a").is_relative_to("") is False
    with pytest.raises(ValueError):
        Uri("http://h/a").relative_to("")


@pytest.mark.skipif(os.name != "nt", reason="Windows drive roots")
def test_file_uri_parent_of_top_level_windows_folder_is_the_drive_root():
    from pathlib_next.uri import UriPath

    parent = UriPath("file:///C:/Windows").parent
    assert parent.path == "C:/"
    assert str(parent.filepath) == "C:\\"
    assert parent.parent == parent


# --- 3.12 matches a root or empty path as an empty line -------------------


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("**", True),
        ("*", False),
        ("?", False),
        ("[ab]", False),
        ("a", False),
        ("a*", False),
        ("*a", False),
        ("**a", False),
    ],
)
def test_matches_empty_line_312_table(pattern, expected):
    """3.12 alone compiles a pattern with separators swapped for newlines,
    so the root -- and an empty path -- is an empty line, and only a part
    that can match "" reaches it. A lone "*" is compiled as ".+" there, so
    it does not, while "**" does. The helper is pure, so its table is
    checked on every version even though `match()` only calls it on 3.12.
    """
    from pathlib_next.path import _matches_empty_line_312

    assert _matches_empty_line_312(pattern, 0) is expected


@pytest.mark.parametrize("cls", GENERIC)
@pytest.mark.parametrize("path", ["/", ""])
def test_generic_match_doublestar_at_the_root_follows_the_interpreter(cls, path):
    """`PurePosixPath("/").match("**")` is True on 3.12 and False on every
    other version; the generic classes must say the same as the running
    interpreter, which is what the 3.12 CI job caught.
    """
    assert _our_match(cls(path), "**") == _oracle_match(path, "**")
