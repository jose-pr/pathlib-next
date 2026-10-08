"""`copy_into()`, `move_into()` and `replace()` on every `Path`."""

from __future__ import annotations

import errno
import os
import pathlib
import sys

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath, MemPathBackend


@pytest.fixture
def tree():
    backend = MemPathBackend()
    root = MemPath("/", backend=backend)
    (root / "src").mkdir()
    (root / "src" / "f.txt").write_text("payload")
    (root / "dst").mkdir()
    return root


# --- copy_into ------------------------------------------------------------


def test_copy_into_copies_under_the_same_name_and_returns_the_new_path(tree):
    new = (tree / "src" / "f.txt").copy_into(tree / "dst")
    assert new == tree / "dst" / "f.txt"
    assert new.read_text() == "payload"
    assert (tree / "src" / "f.txt").read_text() == "payload"


def test_copy_into_takes_a_str_directory_on_the_same_backend(tree):
    new = (tree / "src" / "f.txt").copy_into("/dst")
    assert new.backend is tree.backend
    assert new.read_text() == "payload"


def test_copy_into_has_the_defaults_of_copy(tree):
    source = tree / "src" / "f.txt"
    (tree / "dst" / "f.txt").write_text("old")
    with pytest.raises(FileExistsError):
        source.copy_into(tree / "dst")
    assert (tree / "dst" / "f.txt").read_text() == "old"
    source.copy_into(tree / "dst", overwrite=True)
    assert (tree / "dst" / "f.txt").read_text() == "payload"


def test_copy_into_copies_a_directory_when_asked_to_recurse(tree):
    with pytest.raises(Exception):
        (tree / "src").copy_into(tree / "dst")
    new = (tree / "src").copy_into(tree / "dst", recursive=True)
    assert new == tree / "dst" / "src"
    assert (new / "f.txt").read_text() == "payload"


def test_copy_into_refuses_a_path_without_a_name(tree):
    with pytest.raises(ValueError, match="empty name"):
        tree.copy_into(tree / "dst")


# --- move_into ------------------------------------------------------------


def test_move_into_moves_under_the_same_name_and_returns_the_new_path(tree):
    source = tree / "src" / "f.txt"
    new = source.move_into(tree / "dst")
    assert new == tree / "dst" / "f.txt"
    assert new.read_text() == "payload"
    assert not source.exists()


def test_move_into_takes_a_str_directory_on_the_same_backend(tree):
    new = (tree / "src" / "f.txt").move_into("/dst")
    assert new.backend is tree.backend
    assert new.read_text() == "payload"


def test_move_into_has_the_defaults_of_move(tree):
    source = tree / "src" / "f.txt"
    (tree / "dst" / "f.txt").write_text("old")
    with pytest.raises(FileExistsError):
        source.move_into(tree / "dst")
    assert source.read_text() == "payload"
    source.move_into(tree / "dst", overwrite=True)
    assert (tree / "dst" / "f.txt").read_text() == "payload"
    assert not source.exists()


def test_move_into_refuses_a_path_without_a_name(tree):
    with pytest.raises(ValueError, match="empty name"):
        tree.move_into(tree / "dst")


# --- replace --------------------------------------------------------------


def test_replace_overwrites_an_existing_file_and_returns_the_target(tree):
    source = tree / "src" / "f.txt"
    target = tree / "dst" / "g.txt"
    target.write_text("old")
    assert source.replace(target) == target
    assert target.read_text() == "payload"
    assert not source.exists()


def test_replace_takes_a_str_target_on_the_same_backend(tree):
    new = (tree / "src" / "f.txt").replace("/dst/g.txt")
    assert new == tree / "dst" / "g.txt"
    assert new.backend is tree.backend
    assert new.read_text() == "payload"


def test_replace_keeps_a_directory_that_holds_something(tree):
    (tree / "dst" / "keep.txt").write_text("keep")
    with pytest.raises(OSError) as raised:
        (tree / "src").replace(tree / "dst")
    assert raised.value.errno == errno.ENOTEMPTY
    assert (tree / "dst" / "keep.txt").read_text() == "keep"
    assert (tree / "src" / "f.txt").read_text() == "payload"


def test_replace_puts_a_directory_over_an_empty_one(tree):
    assert (tree / "src").replace(tree / "dst") == tree / "dst"
    assert (tree / "dst" / "f.txt").read_text() == "payload"
    assert not (tree / "src").exists()


def test_replace_does_not_put_a_file_over_a_directory(tree):
    with pytest.raises(IsADirectoryError):
        (tree / "src" / "f.txt").replace(tree / "dst")
    assert (tree / "src" / "f.txt").read_text() == "payload"


# --- LocalPath ------------------------------------------------------------


def _local(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "f.txt").write_text("payload")
    (root / "dst").mkdir()
    return root


def test_localpath_resolves_the_into_methods_to_this_library(tmp_path):
    for name in ("copy_into", "move_into"):
        assert getattr(pathlib_next.LocalPath, name).__module__.startswith(
            "pathlib_next"
        )


def test_localpath_keeps_stdlibs_replace():
    assert pathlib_next.LocalPath.replace.__module__ == "pathlib"
    assert pathlib_next.LocalPath.replace is pathlib.Path.replace


def test_localpath_copy_into_does_what_a_generic_path_does(tmp_path):
    root = _local(tmp_path)
    new = (root / "src" / "f.txt").copy_into(root / "dst")
    assert new == root / "dst" / "f.txt"
    assert new.read_text() == "payload"
    with pytest.raises(FileExistsError):
        (root / "src" / "f.txt").copy_into(root / "dst")
    (root / "src" / "f.txt").copy_into(root / "dst", overwrite=True)
    copied = (root / "src").copy_into(root / "dst", recursive=True)
    assert (copied / "f.txt").read_text() == "payload"


def test_localpath_move_into_returns_the_new_path(tmp_path):
    root = _local(tmp_path)
    new = (root / "src" / "f.txt").move_into(root / "dst")
    assert new == root / "dst" / "f.txt"
    assert new.read_text() == "payload"
    assert not (root / "src" / "f.txt").exists()


def test_localpath_replace_swaps_a_file_in_one_step(tmp_path):
    root = _local(tmp_path)
    target = root / "dst" / "g.txt"
    target.write_text("old")
    result = (root / "src" / "f.txt").replace(target)
    assert pathlib.Path(result) == pathlib.Path(target)
    assert target.read_text() == "payload"


def test_a_downstream_local_class_gets_the_into_methods_of_this_library(tmp_path):
    from pathlib_next.fspath import _BaseFSPathname
    from pathlib_next.path import Path

    class Downstream(
        pathlib.WindowsPath if os.name == "nt" else pathlib.PosixPath,
        Path,
        _BaseFSPathname,
    ):
        __slots__ = ()

    for name in ("copy_into", "move_into"):
        assert getattr(Downstream, name) is getattr(Path, name)
    assert Downstream.replace.__module__ == "pathlib"


@pytest.mark.skipif(sys.version_info < (3, 14), reason="3.14 added the stdlib methods")
def test_the_stdlib_into_methods_do_not_win_on_python_314(tmp_path):
    root = _local(tmp_path)
    (root / "dst" / "f.txt").write_text("old")
    with pytest.raises(FileExistsError):
        (root / "src" / "f.txt").copy_into(root / "dst")
