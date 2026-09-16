"""Differential glob tests: `pathlib.Path.glob()`/`rglob()` of the RUNNING
interpreter is the oracle, compared as sets of relative paths over the same
real tree for LocalPath, a FileUri of it, and a MemPath copy of it.

Expected results legitimately differ by interpreter (a trailing "**" also
selects files on 3.13+), which is why nothing here hard-codes them.
"""

import os
import pathlib
import sys
import time

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.uri import UriPath
from pathlib_next.utils import glob as _glob

_FILES = (
    "a.txt",
    "b.py",
    ".h.txt",
    ".hd/e.py",
    ".hd/deep/f.txt",
    "sub/c.py",
    "sub/nested/d.py",
    "sub/nested/.hn.txt",
    "rep/rep/rep/g.py",
)
_DIRS = ("empty",)

_PATTERNS = [
    "*",
    "*.txt",
    "*.py",
    ".*",
    "?.txt",
    "[ab].*",
    "a.txt",
    "missing",
    "missing/*",
    "a.txt/*",
    "a.txt/**",
    "sub",
    "sub/",
    "sub/*",
    "*/*.py",
    "sub/nested/*",
    "**",
    "sub/**",
    "**/",
    "sub/**/",
    "**/*",
    "**/*.py",
    "**/**/*.py",
    "**/**",
    "**/rep/**/*.py",
    "rep/**/rep/**",
    "**/rep/**/rep/*",
    "**/nested/*",
    "**/.hd/**",
]
# A trailing separator selects directories only from 3.11; older pathlib
# ignored it, while pathlib_next applies it on every version.
_TRAILING_SEP_WILDCARD = ["*/", "sub/*/", "**/*/"]

_RGLOB_PATTERNS = ["*.py", "*", "rep", "nested/*", ".*", ""]


def _build(root: pathlib.Path):
    for rel in _FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    for rel in _DIRS:
        (root / rel).mkdir()


def _rel(path, base) -> str:
    s, b = path.as_posix(), base.as_posix()
    assert s.startswith(b), (s, b)
    return s[len(b) :].lstrip("/") or "."


def _stdlib(root, method, pattern):
    return {_rel(p, root) for p in getattr(root, method)(pattern)}


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    _build(root)
    return root


def _mempath(root: pathlib.Path) -> MemPath:
    base = MemPath("/tree")
    base.mkdir()
    for rel in _FILES:
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rel)
    for rel in _DIRS:
        (base / rel).mkdir()
    return base


_IMPLS = {
    "LocalPath": lambda root: pathlib_next.LocalPath(root),
    "FileUri": lambda root: UriPath(root.as_uri()),
    "MemPath": _mempath,
}


@pytest.fixture(params=sorted(_IMPLS))
def impl(request, tree):
    return _IMPLS[request.param](tree)


def _ours(base, method, pattern, **kwargs):
    found = [_rel(p, base) for p in getattr(base, method)(pattern, **kwargs)]
    assert len(found) == len(set(found)), f"duplicates: {sorted(found)}"
    return set(found)


@pytest.mark.parametrize("pattern", _PATTERNS + _TRAILING_SEP_WILDCARD)
def test_glob_matches_running_pathlib(tree, impl, pattern):
    if pattern in _TRAILING_SEP_WILDCARD and sys.version_info < (3, 11):
        pytest.skip("pathlib < 3.11 ignores a trailing separator")
    assert _ours(impl, "glob", pattern) == _stdlib(tree, "glob", pattern)


@pytest.mark.parametrize("pattern", _RGLOB_PATTERNS)
def test_rglob_matches_running_pathlib(tree, impl, pattern):
    assert _ours(impl, "rglob", pattern) == _stdlib(tree, "rglob", pattern)


def test_trailing_doublestar_follows_interpreter(tree, impl):
    found = _ours(impl, "glob", "sub/**")
    assert ("sub/c.py" in found) is (sys.version_info >= (3, 13))
    assert {"sub", "sub/nested"} <= found


def test_include_hidden_false_filters_and_skips_hidden_dirs(impl):
    found = _ours(impl, "glob", "**/*", include_hidden=False)
    assert not any(part.startswith(".") for p in found for part in p.split("/"))
    assert "sub/nested/d.py" in found
    # A literal hidden component is never filtered.
    assert _ours(impl, "glob", ".hd/*", include_hidden=False) == {
        ".hd/e.py",
        ".hd/deep",
    }


def test_missing_or_file_base_selects_nothing(tree, impl):
    for base in (impl / "missing", impl / "a.txt"):
        for pattern in ("*", "**", "**/*.py", "x/*"):
            assert list(base.glob(pattern)) == []


# --- directory symlink cycles ------------------------------------------------


@pytest.fixture
def loop_tree(tmp_path):
    root = tmp_path / "loop"
    (root / "a").mkdir(parents=True)
    (root / "a" / "f.txt").write_text("f")
    try:
        # Two cycles: before the fix recursion branched 2x per level.
        os.symlink(root, root / "a" / "loop", target_is_directory=True)
        os.symlink(root, root / "a" / "loop2", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    return root


@pytest.mark.parametrize("kind", ["LocalPath", "FileUri"])
@pytest.mark.parametrize(
    "method,pattern",
    [
        ("glob", "**"),
        ("glob", "**/"),
        ("glob", "**/*"),
        ("glob", "**/f.txt"),
        ("glob", "*/loop/*"),
        ("glob", "a/*/"),
        ("rglob", "f.txt"),
        ("rglob", "*"),
    ],
)
def test_glob_does_not_recurse_into_directory_symlinks(
    loop_tree, kind, method, pattern
):
    last = pattern.rstrip("/").split("/")[-1]
    if pattern.endswith("/") and last != "**" and sys.version_info < (3, 11):
        pytest.skip("pathlib < 3.11 ignores a trailing separator")
    base = _IMPLS[kind](loop_tree)
    start = time.monotonic()
    assert _ours(base, method, pattern) == _stdlib(loop_tree, method, pattern)
    assert time.monotonic() - start < 10


# --- pattern validation ------------------------------------------------------


def test_empty_pattern_raises_value_error(impl):
    with pytest.raises(ValueError):
        impl.glob("")
    with pytest.raises(ValueError):
        impl.glob(".")


def test_absolute_pattern_raises_like_pathlib(impl):
    for call in (lambda: impl.glob("/x/*"), lambda: impl.rglob("/x")):
        with pytest.raises(NotImplementedError):
            call()
        with pytest.raises(ValueError):
            call()


def test_localpath_anchored_patterns_raise(tree):
    root = pathlib_next.LocalPath(tree)
    outside = tree.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    patterns = [outside.as_posix() + "/*"]
    if os.name == "nt":
        patterns += ["C:x", "C:/x/*", "\\x\\*"]
    for pattern in patterns:
        with pytest.raises(NotImplementedError):
            list(pathlib.Path(tree).glob(pattern))
        with pytest.raises(NotImplementedError):
            root.glob(pattern)
        with pytest.raises(NotImplementedError):
            root.rglob(pattern)


@pytest.mark.skipif(os.name != "nt", reason="drive syntax is Windows-only")
def test_localpath_drive_inside_pattern_stays_under_self(tree):
    root = pathlib_next.LocalPath(tree)
    assert list(root.glob("sub/C:/Windows/*")) == []
    assert list(root.glob("sub\\*.py")) == [root / "sub" / "c.py"]


def test_recurse_symlinks_true_is_unsupported(impl):
    assert _ours(impl, "glob", "*.py", recurse_symlinks=False) == {"b.py"}
    with pytest.raises(NotImplementedError):
        impl.glob("**", recurse_symlinks=True)


def test_uripath_question_mark_is_a_wildcard(tree):
    (tree / "a1.txt").write_text("1")
    (tree / "a2.txt").write_text("2")
    base = UriPath(tree.as_uri())
    assert {p.name for p in base.glob("a?.txt")} == {"a1.txt", "a2.txt"}
    assert {p.name for p in base.glob("*/n?sted/*.py")} == {"d.py"}


def test_module_glob_excludes_hidden_by_default(tree):
    pattern = pathlib_next.LocalPath(tree) / "**" / "*.py"
    names = {p.name for p in _glob.glob(pattern, recursive=True)}
    assert names == {"b.py", "c.py", "d.py", "g.py"}
    hidden = {p.name for p in _glob.glob(pattern, recursive=True, include_hidden=True)}
    assert hidden == names | {"e.py"}


# --- full_match ----------------------------------------------------------------

_FULL_MATCH_CASES = [
    ("a", "a/**", False),
    ("a/b", "a/**", True),
    ("a/b", "a/b/", True),
    ("a/b", "**", True),
    ("a", "**/a", True),
    ("a/b", "a/**/b", True),
    ("a/b", "**/**", True),
    ("a", "**/**", True),
    ("a/b/c", "a/**/**/c", True),
    ("a/b", "*", False),
    ("a/b", "*/*", True),
    ("/a/b", "/a/**", True),
    ("a/.b", "a/*", True),
    ("a/b/c", "**/c/**", False),
    ("a/b", "a/b/**", False),
    ("a/b", "**/b/", True),
]


@pytest.mark.parametrize("path,pattern,expected", _FULL_MATCH_CASES)
def test_full_match_semantics(path, pattern, expected):
    assert _glob.full_match(path.split("/"), pattern, True) is expected
    if sys.version_info >= (3, 13):
        assert pathlib.PurePosixPath(path).full_match(pattern) is expected


def test_full_match_repeated_doublestar_is_not_exponential():
    segments = ["x"] * 24
    pattern = "/".join(["**", "x"] * 8) + "/nomatch"
    start = time.monotonic()
    assert not _glob.full_match(segments, pattern, True)
    assert _glob.full_match(segments, "/".join(["**", "x"] * 8), True)
    assert time.monotonic() - start < 1


# --- glob(None): expand the pattern this path carries ---------------------


@pytest.mark.parametrize("cls", ["local", "uri"])
def test_glob_none_expands_the_pattern_the_path_carries(tree, cls):
    """`glob("")` raises like pathlib; `glob(None)` is the supported way to
    expand a path that IS a pattern -- what yaconfiglib's `glob("")` did
    before 0.9.4."""
    root = pathlib_next.LocalPath(tree)
    carrier = root / "*.py" if cls == "local" else UriPath((root / "*.py").as_uri())
    got = sorted(p.name for p in carrier.glob(None))
    assert got == sorted(p.name for p in root.glob("*.py"))
    assert got


def test_glob_none_recursive_and_rglob_none(tree):
    root = pathlib_next.LocalPath(tree)
    carrier = root / "**" / "*.py"
    expected = sorted(p.name for p in root.rglob("*.py"))
    assert sorted(p.name for p in carrier.glob(None, recursive=True)) == expected
    assert sorted(p.name for p in carrier.rglob(None)) == expected


def test_glob_empty_string_still_raises(tree):
    """The parity fix stands: only `None` opts into the carried-pattern
    form, so `glob("")` keeps raising exactly what pathlib raises."""
    root = pathlib_next.LocalPath(tree)
    with pytest.raises(ValueError):
        list(root.glob(""))
    with pytest.raises(ValueError):
        list(pathlib.Path(tree).glob(""))


# --- native=: follow the interpreter, or one rule everywhere --------------


def test_native_true_matches_the_running_interpreter_for_a_trailing_slash(tree):
    """Before 3.11 pathlib ignores a trailing separator; from 3.11 it selects
    directories only. The default follows whichever is running."""
    root = pathlib_next.LocalPath(tree)
    ours = {p.name for p in root.glob("*/")}
    stdlib = {p.name for p in pathlib.Path(tree).glob("*/")}
    assert ours == stdlib


def test_native_false_selects_directories_on_every_version(tree):
    root = pathlib_next.LocalPath(tree)
    got = {p.name for p in root.glob("*/", native=False)}
    assert got == {p.name for p in root.iterdir() if p.is_dir()}


def test_native_true_follows_the_interpreter_for_a_partial_doublestar(tree):
    """ "a**" is a plain wildcard from 3.13 and a ValueError before it."""
    root = pathlib_next.LocalPath(tree)
    if sys.version_info >= (3, 13):
        assert {p.name for p in root.glob("**.py")} == {
            p.name for p in pathlib.Path(tree).glob("**.py")
        }
    else:
        with pytest.raises(ValueError, match="entire path component"):
            list(root.glob("**.py"))
        with pytest.raises(ValueError, match="entire path component"):
            list(pathlib.Path(tree).glob("**.py"))


def test_native_false_treats_a_partial_doublestar_as_a_wildcard(tree):
    root = pathlib_next.LocalPath(tree)
    assert {p.name for p in root.glob("**.py", native=False)} == {
        p.name for p in root.glob("*.py", native=False)
    }


@pytest.mark.parametrize("native", [True, False])
def test_native_is_accepted_by_every_generic_backend(tree, native):
    """The knob is on `Path`, so every backend takes it -- a MemPath has no
    "running interpreter" of its own, and must still answer."""
    mem = MemPath("/m")
    mem.mkdir(parents=True)
    (mem / "x.py").write_text("x")
    (mem / "d").mkdir()
    assert {p.name for p in mem.glob("*.py", native=native)} == {"x.py"}
    assert {p.name for p in mem.glob("*", native=native)} == {"x.py", "d"}


# --- on_error: an unreadable directory can be seen ------------------------


@pytest.fixture
def unreadable_tree(tmp_path, monkeypatch):
    """A tree whose `locked/` directory refuses to list, without needing
    real permissions (which a Windows admin ignores anyway)."""
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "a.json").write_text("{}")
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "b.json").write_text("{}")
    real = pathlib_next.LocalPath._scandir

    def refusing(self):
        if self.name == "locked":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(pathlib_next.LocalPath, "_scandir", refusing)
    return tmp_path


def test_a_failed_listing_is_silent_without_the_hook(unreadable_tree):
    """pathlib's behaviour, and the default: the layer simply vanishes."""
    root = pathlib_next.LocalPath(unreadable_tree)
    assert sorted(p.name for p in root.glob("**/*.json")) == ["a.json"]


def test_on_error_reports_the_failed_listing_and_keeps_going(unreadable_tree):
    root = pathlib_next.LocalPath(unreadable_tree)
    seen = []
    found = sorted(p.name for p in root.glob("**/*.json", on_error=seen.append))
    assert found == ["a.json"]
    assert seen and all(isinstance(e, PermissionError) for e in seen)
    # One argument is enough to say where, as with `walk()`/`os.walk`.
    assert all(pathlib.Path(e.filename).name == "locked" for e in seen)


def test_on_error_may_raise_to_make_it_fatal(unreadable_tree):
    """The point of the hook: a caller who wants the error back gets it."""
    root = pathlib_next.LocalPath(unreadable_tree)

    def reraise(error):
        raise error

    with pytest.raises(PermissionError):
        list(root.glob("**/*.json", on_error=reraise))
    with pytest.raises(PermissionError):
        list(root.rglob("*.json", on_error=reraise))


# --- bound_loops: a directory is walked once per "**" ---------------------


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows")
def test_bound_loops_bounds_a_windows_junction_loop(tmp_path):
    """A junction reports `is_symlink() == False`, so `recurse_symlinks`
    cannot see it and `**` walks the loop until the recursion limit --
    pathlib does the same. Identity bounds it."""
    import _winapi

    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x.json").write_text("{}")
    _winapi.CreateJunction(str(tmp_path / "b"), str(tmp_path / "b" / "self"))
    base = pathlib_next.LocalPath(tmp_path)

    # Unbounded, the one file is reached again through every extra "self"
    # (pathlib does the same, and on 3.9 it eventually raises WinError 1921
    # on the over-long path -- which is why stdlib is not the oracle here).
    assert len(list(base.glob("b/**/*.json"))) > 1
    assert len(list(base.glob("b/**/*.json", bound_loops=True))) == 1
    assert len(list((base / "b").rglob("*.json", bound_loops=True))) == 1


def test_bound_loops_keeps_a_plain_tree_intact(tree):
    """Bounding must not drop ordinary directories: every match the default
    finds is still found."""
    root = pathlib_next.LocalPath(tree)
    plain = sorted(str(p) for p in root.glob("**/*.py"))
    assert sorted(str(p) for p in root.glob("**/*.py", bound_loops=True)) == plain


def test_bound_loops_is_accepted_where_stats_have_no_identity():
    """A backend whose stat carries no (st_dev, st_ino) cannot be bounded;
    it must still answer rather than raise."""
    mem = MemPath("/m")
    mem.mkdir(parents=True)
    (mem / "s").mkdir()
    (mem / "s" / "x.py").write_text("x")
    assert [str(p) for p in mem.glob("**/*.py", bound_loops=True)] == ["/m/s/x.py"]
