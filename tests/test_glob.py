"""glob() against stdlib ground truth (pathlib.Path.glob, which pathlib_next
tries to match) on a real filesystem tree, plus dedicated regression checks
for the glob-engine bugs found in Phases 2-4 (B7 wildcard detection, B18
dironly, the ** auto-recursion parity gap) and coverage of the shared
utils/glob.py engine via MemPath (a non-pathlib-backed consumer).
"""
import pathlib

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath


def _names(paths):
    return {p.name for p in paths}


@pytest.mark.parametrize(
    "pattern",
    ["*.py", "*.txt", "a.txt", "*", "b*", "*.tar.gz"],
)
def test_glob_matches_stdlib_pathlib(fixture_tree, pattern):
    ours = _names(pathlib_next.LocalPath(fixture_tree).glob(pattern))
    theirs = _names(pathlib.Path(fixture_tree).glob(pattern))
    # pathlib.Path.glob() only gained include_hidden= (default False) in
    # 3.13; before that it always included dotfiles. Our include_hidden=
    # default (False) matches 3.13+, so on older stdlib pathlib we must
    # drop hidden entries from the ground truth ourselves to compare like
    # for like.
    theirs = {n for n in theirs if not n.startswith(".")}
    assert ours == theirs


def test_glob_trailing_wildcard_b7(fixture_tree):
    # B7 regression: WILCARD_PATTERN.match (anchored) missed "foo*"-style
    # trailing wildcards, so a pattern like "b*" wasn't even recognized as
    # containing a wildcard (has_glob_pattern) prior to the fix.
    root = pathlib_next.LocalPath(fixture_tree)
    assert root.joinpath("b*").has_glob_pattern()
    assert _names(root.glob("b*")) == {"b.py"}


def test_glob_recursive_star_star(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    ours = _names(root.glob("**/*.py"))
    theirs = _names(pathlib.Path(fixture_tree).glob("**/*.py"))
    assert ours == theirs == {"b.py", "c.py", "d.py"}


def test_glob_recursive_false_disables_even_with_star_star(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    # Explicit recursive=False must win over "**" auto-detection.
    names = _names(root.glob("**/*.py", recursive=False))
    assert "d.py" not in names  # nested/d.py only reachable recursively


def test_glob_include_hidden(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    assert ".hidden.txt" not in _names(root.glob("*.txt"))
    assert ".hidden.txt" in _names(root.glob("*.txt", include_hidden=True))


def test_glob_dironly_trailing_slash_b18(fixture_tree):
    # B18 regression: dironly default False (not None) made the trailing-
    # slash-implies-dironly detection in LocalPath.glob dead code.
    root = pathlib_next.LocalPath(fixture_tree)
    results = list(root.glob("sub/"))
    assert all(p.is_dir() for p in results)
    assert _names(results) == {"sub"}


def test_glob_empty_dir_yields_nothing(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    assert list((root / "empty_dir").glob("*")) == []


def test_glob_no_match_yields_nothing(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    assert list(root.glob("*.nonexistent")) == []


def test_glob_on_mempath():
    backend = None
    root = MemPath("/")
    (root / "a.py").write_text("a")
    (root / "b.txt").write_text("b")
    (root / "sub").mkdir()
    (root / "sub" / "c.py").write_text("c")
    assert _names(root.glob("*.py")) == {"a.py"}
    assert _names(root.glob("**/*.py")) == {"a.py", "c.py"}
