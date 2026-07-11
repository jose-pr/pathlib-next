"""LocalPath vs pathlib.Path on a real tmp_path tree. LocalPath *is*
pathlib.WindowsPath/PosixPath with our Path mixed in via MRO, so most of
these just confirm we haven't broken pathlib's own behavior, plus parity
for the handful of methods LocalPath explicitly overrides (touch, mkdir,
glob, rm/copy/move which have no direct pathlib.Path equivalent to diverge
from pre-3.14).
"""
import os

import pytest

import pathlib_next


def test_touch_exist_ok_false_raises(tmp_path):
    p = pathlib_next.LocalPath(tmp_path) / "f.txt"
    p.touch()
    with pytest.raises(FileExistsError):
        p.touch(exist_ok=False)
    # B17 regression: must not have truncated the existing content.
    p.write_text("keep me")
    with pytest.raises(FileExistsError):
        p.touch(exist_ok=False)
    assert p.read_text() == "keep me"


def test_touch_creates_new_file(tmp_path):
    p = pathlib_next.LocalPath(tmp_path) / "new.txt"
    assert not p.exists()
    p.touch(exist_ok=False)
    assert p.exists()
    assert p.read_text() == ""


def test_mkdir_parents_exist_ok(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "a").mkdir()
    # B16 regression: parents=True must not choke on an already-existing
    # parent, and must still honor exist_ok=False for the leaf.
    (root / "a" / "b" / "c").mkdir(parents=True, exist_ok=True)
    assert (root / "a" / "b" / "c").is_dir()
    with pytest.raises(FileExistsError):
        (root / "a" / "b" / "c").mkdir(parents=True, exist_ok=False)


def test_glob_matches_pathlib(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    stdlib_root = fixture_tree
    ours = {p.name for p in root.glob("*.py")}
    theirs = {p.name for p in stdlib_root.glob("*.py")}
    assert ours == theirs == {"b.py"}


def test_glob_recursive_auto_enable(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    # B-adjacent parity gap (Phase 4): "**" in the pattern auto-enables
    # recursion without passing recursive=True explicitly.
    ours = {p.name for p in root.glob("**/*.py")}
    theirs = {p.name for p in fixture_tree.glob("**/*.py")}
    assert ours == theirs == {"b.py", "c.py", "d.py"}


def test_glob_hidden_excluded_by_default(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    names = {p.name for p in root.glob("*.txt")}
    assert names == {"a.txt"}  # .hidden.txt excluded


def test_walk_matches_os_walk(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    ours = sorted(
        (str(p.relative_to(root).as_posix() if p != root else "."), sorted(d), sorted(f))
        for p, d, f in root.walk()
    )
    theirs = sorted(
        (
            os.path.relpath(dirpath, fixture_tree).replace(os.sep, "/"),
            sorted(dirnames),
            sorted(filenames),
        )
        for dirpath, dirnames, filenames in os.walk(fixture_tree)
    )
    assert ours == theirs


def test_walk_top_down_false(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    ours_order = [p.name if p != root else "." for p, _, _ in root.walk(top_down=False)]
    theirs_order = [
        os.path.basename(dirpath) or "."
        for dirpath, _, _ in os.walk(fixture_tree, topdown=False)
    ]
    assert len(ours_order) == len(theirs_order)
    # bottom-up: deepest directories must come before their parents.
    assert ours_order.index("nested") < ours_order.index("sub")


def test_rm_recursive(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    (root / "sub").rm(recursive=True)
    assert not (fixture_tree / "sub").exists()
    assert (fixture_tree / "a.txt").exists()


def test_rm_missing_ok(tmp_path):
    p = pathlib_next.LocalPath(tmp_path) / "missing"
    with pytest.raises(FileNotFoundError):
        p.rm()
    p.rm(missing_ok=True)


def test_rm_ignore_error_callable_invoked(tmp_path):
    # B14 regression: ignore_error callable must actually be called.
    calls = []
    p = pathlib_next.LocalPath(tmp_path) / "missing"
    p.rm(ignore_error=lambda err, path: calls.append((type(err), path)) or True)
    assert len(calls) == 1
    assert calls[0][0] is FileNotFoundError


def test_copy_into_existing_dir_raises(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "d").mkdir()
    (root / "f.txt").write_text("x")
    with pytest.raises(IsADirectoryError):
        (root / "f.txt").copy(root / "d")


def test_copy_overwrite(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "src.txt").write_text("src")
    (root / "dst.txt").write_text("dst")
    with pytest.raises(FileExistsError):
        (root / "src.txt").copy(root / "dst.txt")
    (root / "src.txt").copy(root / "dst.txt", overwrite=True)
    assert (root / "dst.txt").read_text() == "src"


def test_move_rename_fallback(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "src.txt").write_text("data")
    (root / "src.txt").move(root / "dst.txt")
    assert not (root / "src.txt").exists()
    assert (root / "dst.txt").read_text() == "data"
