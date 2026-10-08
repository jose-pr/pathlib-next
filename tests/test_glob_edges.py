"""Glob edge cases, each compared with the `pathlib` of the RUNNING
interpreter over the same tree (`LocalPath`, and a `MemPath` copy where the
rule is not about the local system), plus what `pathlib` does not answer:
listings per directory, `on_error`, audit events and loop bounds."""

import collections
import json
import os
import pathlib
import subprocess
import sys

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.utils import glob as _glob
from pathlib_next.utils.stat import FileStat

WINDOWS = os.name == "nt"

_FILES = (
    "a.txt",
    "b.py",
    "B2.PY",
    ".hidden.txt",
    ".hdir/inner.py",
    "sub/c.py",
    "sub/.dot.py",
    "sub/nested/d.py",
    "sub/nested/deep/e.py",
    "sub2/sub/c.py",
)
_DIRS = ("empty_dir",)


def _build(root: pathlib.Path):
    for rel in _FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(rel)
    for rel in _DIRS:
        (root / rel).mkdir()


def _mem_twin(root: pathlib.Path) -> MemPath:
    twin = MemPath("/tree")
    twin.mkdir()
    for rel in _FILES:
        target = twin / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rel)
    for rel in _DIRS:
        (twin / rel).mkdir()
    return twin


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    _build(root)
    return root


def _relative(path, base) -> str:
    text, prefix = path.as_posix(), base.as_posix()
    assert text.startswith(prefix), (text, prefix)
    return text[len(prefix) :].lstrip("/") or "."


def _answer(base, method, pattern, kwargs):
    """What a call selects, as the paths relative to `base`, or what it
    raised (a `NonRelativePatternError` is the `NotImplementedError` that
    pathlib raises)."""
    try:
        return "ok", {
            _relative(p, base) for p in getattr(base, method)(pattern, **kwargs)
        }
    except Exception as error:  # noqa: BLE001
        if isinstance(error, NotImplementedError):
            return "raised", "NotImplementedError"
        return "raised", type(error).__name__


def _compare(tree, impl_base, method, pattern, **kwargs):
    oracle = _answer(tree, method, pattern, kwargs)
    assert _answer(impl_base, method, pattern, kwargs) == oracle, (
        method,
        pattern,
        kwargs,
    )


# --- the anchor of an extended-length path is a prefix, not a wildcard ------


@pytest.mark.skipif(not WINDOWS, reason="extended-length paths are a Windows spelling")
def test_a_carried_pattern_under_an_extended_length_anchor_is_expanded(tree):
    plain = pathlib_next.LocalPath(tree, "*.py")
    extended = pathlib_next.LocalPath("\\\\?\\" + str(tree) + "\\*.py")

    found = sorted(p.name for p in extended.glob(None))

    assert found == sorted(p.name for p in plain.glob(None)) == ["B2.PY", "b.py"]
    assert extended.has_glob_pattern() is plain.has_glob_pattern()


@pytest.mark.skipif(not WINDOWS, reason="extended-length paths are a Windows spelling")
def test_a_carried_extended_length_path_without_a_wildcard_is_found(tree):
    extended = pathlib_next.LocalPath("\\\\?\\" + str(tree / "b.py"))
    assert [p.name for p in extended.glob(None)] == ["b.py"]


# --- an explicit case_sensitive changes the case rule and nothing else ------

_CASE_PATTERNS = [
    ("..", True),
    ("../*", True),
    ("*/..", True),
    ("sub/../*", True),
    ("sub/nested/..", True),
    ("**/..", True),
    ("a.txt", True),
    ("A.TXT", True),
    ("A.TXT", False),
    ("SUB", False),
    ("Sub/*.PY", False),
    (" ", False),
    ("nul", False),
]


@pytest.mark.skipif(
    sys.version_info < (3, 12), reason="pathlib takes case_sensitive from 3.12"
)
@pytest.mark.parametrize("pattern, case_sensitive", _CASE_PATTERNS)
def test_explicit_case_sensitive_matches_pathlib(tree, pattern, case_sensitive):
    _compare(
        tree,
        pathlib_next.LocalPath(tree),
        "glob",
        pattern,
        case_sensitive=case_sensitive,
    )
    _compare(
        tree,
        pathlib_next.LocalPath(tree),
        "rglob",
        pattern,
        case_sensitive=case_sensitive,
    )


def test_dot_dot_stays_literal_when_the_case_rule_is_given():
    base = MemPath("/root")
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "f.txt").write_text("x")

    for case_sensitive in (True, False):
        found = {
            p.as_posix()
            for p in base.glob("sub/../sub/*", case_sensitive=case_sensitive)
        }
        assert found == {"/root/sub/../sub/f.txt"}


# --- bound_loops needs a real identity --------------------------------------


class _Status:
    def __init__(self, stat, dev, ino):
        self._stat = stat
        self.st_dev, self.st_ino = dev, ino
        self.st_mode, self.st_size, self.st_mtime = (
            stat.st_mode,
            stat.st_size,
            stat.st_mtime,
        )

    def __getattr__(self, name):
        return getattr(self._stat, name)


def _identified(dev, ino):
    class Identified(MemPath):
        __slots__ = ()

        def stat(self, *, follow_symlinks=True):
            return _Status(super().stat(follow_symlinks=follow_symlinks), dev, ino)

    return Identified


@pytest.mark.parametrize("ino", [0, None])
def test_bound_loops_does_not_drop_directories_on_a_stat_without_an_inode(ino):
    root = _identified(7, ino)("/r")
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "deep.conf").write_text("x")
    (root / "top.conf").write_text("x")

    free = sorted(p.as_posix() for p in root.glob("**/*.conf"))
    bounded = sorted(p.as_posix() for p in root.glob("**/*.conf", bound_loops=True))

    assert bounded == free == ["/r/a/b/deep.conf", "/r/top.conf"]


def test_bound_loops_still_skips_a_directory_that_is_its_own_ancestor():
    class Inodes(MemPath):
        __slots__ = ()

        def stat(self, *, follow_symlinks=True):
            stat = super().stat(follow_symlinks=follow_symlinks)
            # `loop` is another name for its parent: the same identity.
            named = self.parent.as_posix() if self.name == "loop" else self.as_posix()
            return _Status(stat, 7, 1000 + sum(map(ord, named)))

    root = Inodes("/r")
    (root / "a" / "loop").mkdir(parents=True)
    (root / "a" / "loop" / "again.conf").write_text("x")
    (root / "a" / "f.conf").write_text("x")

    free = sorted(p.as_posix() for p in root.glob("**/*.conf"))
    bounded = sorted(p.as_posix() for p in root.glob("**/*.conf", bound_loops=True))

    assert free == ["/r/a/f.conf", "/r/a/loop/again.conf"]
    assert bounded == ["/r/a/f.conf"]


# --- a literal component is checked as the running pathlib checks it --------

_LITERAL_PATTERNS = [
    "a.txt/..",
    "sub/c.py/..",
    "nonexistent/..",
    "nonexistent/*",
    "**/deep/..",
    "**/nonexistent/..",
    "sub/nested/..",
    "sub/nested/../*",
    "sub/../*",
    "../*",
    "a.txt/**",
]


@pytest.mark.parametrize("method", ["glob", "rglob"])
@pytest.mark.parametrize("pattern", _LITERAL_PATTERNS)
def test_localpath_checks_literals_like_the_running_pathlib(tree, method, pattern):
    _compare(tree, pathlib_next.LocalPath(tree), method, pattern)


@pytest.mark.parametrize("method", ["glob", "rglob"])
@pytest.mark.parametrize("pattern", _LITERAL_PATTERNS)
def test_mempath_checks_literals_like_the_running_pathlib(tree, method, pattern):
    if WINDOWS and sys.version_info >= (3, 13) and ".." in pattern:
        pytest.skip(
            "pathlib 3.13+ tests the joined text, which Windows resolves "
            "lexically; MemPath follows the tree"
        )
    _compare(tree, _mem_twin(tree), method, pattern)


def test_a_carried_pattern_checks_its_literals_like_pathlib(tree):
    # `glob(None)` splits at the first wildcard; the literals before it are
    # the base, the ones after it are checked as a pattern's are.
    for tail in ("a.txt/..", "sub/c.py/..", "nonexistent/.."):
        carried = pathlib_next.LocalPath(tree, tail)
        oracle = {
            p.as_posix() for p in pathlib.Path(tree, *tail.split("/")[:-1]).glob("..")
        }
        assert {p.as_posix() for p in carried.glob(None)} == oracle


# --- rglob(".") is every entry below, as pathlib's is -----------------------


@pytest.mark.parametrize("pattern", [".", "./", "./."])
def test_rglob_of_a_pattern_with_no_component_matches_pathlib(tree, pattern):
    _compare(tree, pathlib_next.LocalPath(tree), "rglob", pattern)
    _compare(tree, _mem_twin(tree), "rglob", pattern)


def test_rglob_still_refuses_an_absolute_pattern(tree):
    for base in (pathlib_next.LocalPath(tree), _mem_twin(tree)):
        with pytest.raises(NotImplementedError):
            base.rglob("/x")


def test_glob_of_a_pattern_with_no_component_is_a_value_error(tree):
    with pytest.raises(ValueError):
        pathlib_next.LocalPath(tree).glob(".")
    with pytest.raises(ValueError):
        _mem_twin(tree).glob("")


# --- the last literal is tested as the running pathlib tests it -------------


@pytest.fixture
def links(tree):
    try:
        os.symlink(tree / "gone", tree / "dangling")
        os.symlink(tree / "gone_dir", tree / "dangling_dir", True)
        os.symlink(tree / "a.txt", tree / "link_file")
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks cannot be created here")
    return tree


@pytest.mark.parametrize(
    "pattern",
    ["dangling", "dangling_dir", "link_file", "dangling/", "d*"],
)
def test_a_literal_naming_a_dangling_link_matches_pathlib(links, pattern):
    for method in ("glob", "rglob"):
        _compare(links, pathlib_next.LocalPath(links), method, pattern)


# --- an unlistable base is reported, whatever the first component is --------


def _reported(base, method, pattern):
    seen = []
    list(getattr(base, method)(pattern, on_error=seen.append))
    return [type(error) for error in seen]


@pytest.mark.parametrize("pattern", ["*", "**", "**/*", "x/*", "*/x"])
def test_on_error_hears_of_a_missing_base_for_every_first_component(tmp_path, pattern):
    for base in (
        pathlib_next.LocalPath(tmp_path, "missing"),
        MemPath("/missing"),
    ):
        assert _reported(base, "glob", pattern) == [FileNotFoundError]


@pytest.mark.parametrize("pattern", ["*", "**", "**/*"])
def test_on_error_hears_of_a_file_used_as_a_base(tree, pattern):
    for base in (
        pathlib_next.LocalPath(tree, "a.txt"),
        _mem_twin(tree) / "a.txt",
    ):
        assert _reported(base, "glob", pattern) == [NotADirectoryError]


def test_on_error_is_not_told_about_a_base_that_lists(tree):
    for base in (pathlib_next.LocalPath(tree), _mem_twin(tree)):
        assert _reported(base, "glob", "**") == []
        assert _reported(base, "glob", "**/*.py") == []


# --- a recursive glob lists each directory once -----------------------------


class _Listed(MemPath):
    calls = collections.Counter()

    def _scandir(self):
        type(self).calls[self.as_posix()] += 1
        return super()._scandir()


class _Unreadable(_Listed):
    def _scandir(self):
        type(self).calls[self.as_posix()] += 1
        if self.name == "locked":
            raise PermissionError(13, "denied", str(self))
        return MemPath._scandir(self)


def _listed_tree(cls):
    cls.calls = collections.Counter()
    root = cls("/r")
    root.mkdir()
    for directory in ("a", "a/b", "c", "locked"):
        (root / directory).mkdir()
        (root / directory / "f.txt").write_text("x")
    (root / "top.txt").write_text("x")
    return root


@pytest.mark.parametrize(
    "method, pattern", [("glob", "**/*.txt"), ("glob", "**/*"), ("rglob", "*")]
)
def test_a_recursive_glob_lists_every_directory_once(method, pattern):
    root = _listed_tree(_Listed)
    _Listed.calls.clear()

    found = list(getattr(root, method)(pattern))

    assert len(found) >= 4
    assert set(_Listed.calls.values()) == {1}
    assert set(_Listed.calls) == {"/r", "/r/a", "/r/a/b", "/r/c", "/r/locked"}


@pytest.mark.parametrize("method, pattern", [("glob", "**/*.txt"), ("rglob", "*")])
def test_a_directory_that_cannot_be_listed_is_reported_once(method, pattern):
    root = _listed_tree(_Unreadable)
    seen = []

    list(getattr(root, method)(pattern, on_error=seen.append))

    assert [(type(e), e.filename) for e in seen] == [(PermissionError, "/r/locked")]


def test_the_listing_a_recursive_step_hands_on_is_the_directorys_own():
    root = _listed_tree(_Listed)
    found = sorted(p.as_posix() for p in root.glob("**/*.txt"))
    assert found == [
        "/r/a/b/f.txt",
        "/r/a/f.txt",
        "/r/c/f.txt",
        "/r/locked/f.txt",
        "/r/top.txt",
    ]


# --- two "**" reach a path along several splits; it is yielded once ---------


@pytest.mark.parametrize("pattern", ["**/*/**/*", "**/**/*/*"])
def test_two_recursive_components_yield_a_path_once(tree, pattern):
    found = [p.as_posix() for p in pathlib_next.LocalPath(tree).glob(pattern)]
    assert len(found) == len(set(found))
    assert set(found) == {p.as_posix() for p in tree.glob(pattern)}


# --- a carried pattern is validated when it is carried ----------------------


@pytest.mark.skipif(
    sys.version_info >= (3, 13), reason="a partial ** is a plain wildcard from 3.13"
)
def test_a_carried_partial_recursive_component_raises_at_the_call(tree):
    carried = pathlib_next.LocalPath(tree, "a**")
    with pytest.raises(ValueError):
        carried.glob(None)
    with pytest.raises(ValueError):
        pathlib_next.LocalPath(tree, "sub", "a**").glob(None, recursive=True)
    assert [p.name for p in carried.glob(None, native=False)] == ["a.txt"]


def test_a_carried_pattern_selects_lazily(tree):
    carried = pathlib_next.LocalPath(tree, "*.py")
    selection = carried.glob(None)
    assert iter(selection) is selection
    assert sorted(p.name for p in selection) == sorted(
        p.name for p in tree.glob("*.py")
    )


def test_an_absolute_pattern_is_a_notimplementederror_and_a_valueerror(tree):
    with pytest.raises(_glob.NonRelativePatternError) as info:
        pathlib_next.LocalPath(tree).glob("/abs")
    assert isinstance(info.value, NotImplementedError)
    assert isinstance(info.value, ValueError)


# --- the audit events pathlib raises ----------------------------------------

_AUDIT_SCRIPT = """
import json, os, pathlib, sys
events = []
sys.addaudithook(
    lambda event, args: events.append([event, *map(str, args)])
    if event.startswith("pathlib.Path.") else None
)
root = sys.argv[1]
sys.path.insert(0, sys.argv[2])
from pathlib_next import LocalPath
calls = [
    ("glob", "*.py"), ("rglob", "*.py"), ("glob", "**/*"), ("glob", ""),
    ("rglob", ""), ("glob", "/x"), ("rglob", "sub/*"), ("glob", "sub/"),
]
out = {}
for cls in (pathlib.Path, LocalPath):
    for method, pattern in calls:
        events.clear()
        try:
            list(getattr(cls(root), method)(pattern))
        except Exception:
            pass
        out.setdefault(f"{method}({pattern!r})", []).append(
            [[e[0], os.path.normpath(e[1]), *e[2:]] for e in events]
        )
print(json.dumps(out))
"""


def test_localpath_raises_the_audit_events_pathlib_raises(tree):
    src = str(pathlib.Path(pathlib_next.__file__).resolve().parents[1])
    done = subprocess.run(
        [sys.executable, "-c", _AUDIT_SCRIPT, str(tree), src],
        capture_output=True,
        text=True,
        cwd=str(tree.parent),
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    for call, (stdlib, ours) in json.loads(done.stdout).items():
        assert stdlib, f"pathlib raised no event for {call}"
        assert ours == stdlib, call
