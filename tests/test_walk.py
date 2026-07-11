import pytest
import os
import posixpath as _posix
import unittest.mock
from pathlib_next.mempath import MemPath
from pathlib_next.fspath import LocalPath


def populate_mempath_tree(root: MemPath):
    (root / "a.txt").write_text("a")
    (root / "b.py").write_text("b")
    (root / ".hidden.txt").write_text("hidden")
    sub = root / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("c")
    nested = sub / "nested"
    nested.mkdir()
    (nested / "d.py").write_text("d")
    (root / "empty_dir").mkdir()


def clean_path(p):
    s = p.as_posix()
    if s.startswith("//"):
        return s[1:]
    return s


def test_mempath_walk_top_down():
    root = MemPath("/")
    populate_mempath_tree(root)

    results = list(root.walk(top_down=True))
    results_mapped = [
        (clean_path(p), sorted(dirs), sorted(files)) for p, dirs, files in results
    ]
    
    assert ("/", ["empty_dir", "sub"], [".hidden.txt", "a.txt", "b.py"]) in results_mapped
    assert ("/sub", ["nested"], ["c.py"]) in results_mapped
    assert ("/sub/nested", [], ["d.py"]) in results_mapped
    assert ("/empty_dir", [], []) in results_mapped
    assert len(results_mapped) == 4


def test_mempath_walk_bottom_up():
    root = MemPath("/")
    populate_mempath_tree(root)

    results = list(root.walk(top_down=False))
    results_mapped = [
        (clean_path(p), sorted(dirs), sorted(files)) for p, dirs, files in results
    ]
    assert len(results_mapped) == 4
    assert results_mapped[-1] == ("/", ["empty_dir", "sub"], [".hidden.txt", "a.txt", "b.py"])


def test_mempath_walk_pruning():
    root = MemPath("/")
    populate_mempath_tree(root)

    walked = []
    for path, dirnames, filenames in root.walk(top_down=True):
        walked.append(clean_path(path))
        if "sub" in dirnames:
            dirnames.remove("sub")

    assert "/sub" not in walked
    assert "/sub/nested" not in walked
    assert "/" in walked
    assert "/empty_dir" in walked


def test_mempath_walk_on_error():
    root = MemPath("/")
    populate_mempath_tree(root)

    errors = []
    def on_error(err):
        errors.append(err)

    original_iterdir = MemPath.iterdir
    def mocked_iterdir(self):
        # Match "sub" or "/sub" or "//sub"
        if clean_path(self) == "/sub":
            raise PermissionError("Access denied")
        return original_iterdir(self)

    with unittest.mock.patch.object(MemPath, "iterdir", mocked_iterdir):
        results = list(root.walk(on_error=on_error))
        assert len(errors) == 1
        assert isinstance(errors[0], PermissionError)
        walked_paths = [clean_path(p) for p, _, _ in results]
        assert "/" in walked_paths
        assert "/sub" not in walked_paths
        assert "/sub/nested" not in walked_paths


def test_localpath_walk_parity(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")

    path_local = LocalPath(tmp_path)
    
    our_walk = list(path_local.walk(top_down=True))
    our_walk_mapped = [
        (os.path.relpath(str(p), str(tmp_path)).replace("\\", "/"), sorted(dirs), sorted(files))
        for p, dirs, files in our_walk
    ]

    os_walk = list(os.walk(tmp_path))
    os_walk_mapped = [
        (os.path.relpath(str(p), str(tmp_path)).replace("\\", "/"), sorted(dirs), sorted(files))
        for p, dirs, files in os_walk
    ]

    assert our_walk_mapped == os_walk_mapped
