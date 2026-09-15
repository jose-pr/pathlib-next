"""Generic copy/move/rm must never destroy data they were not asked to.

Each test asserts what has to SURVIVE (the destination, the source, files
outside the tree) -- "an exception was raised" alone passed against the
defects these guard: a failed copy emptied its target, a same-file copy or
case-only move deleted the file, move(overwrite=True) removed the target
before discovering the source was missing, and rm(recursive=True) walked
through Windows junctions and file: directory symlinks.
"""

import os
import subprocess
import sys

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath

LocalPath = pathlib_next.LocalPath


# --- copy -----------------------------------------------------------------


def test_copy_missing_source_leaves_existing_target_intact(tmp_path):
    target = LocalPath(tmp_path / "dst.txt")
    target.write_text("PRECIOUS")
    with pytest.raises(FileNotFoundError):
        LocalPath(tmp_path / "missing.txt").copy(target, overwrite=True)
    assert target.read_text() == "PRECIOUS"


def test_copy_missing_source_creates_no_empty_target(tmp_path):
    target = LocalPath(tmp_path / "new.txt")
    with pytest.raises(FileNotFoundError):
        LocalPath(tmp_path / "missing.txt").copy(target)
    assert not target.exists()


def test_copy_directory_without_recursive_leaves_target_intact(tmp_path):
    src = LocalPath(tmp_path / "adir")
    src.mkdir()
    target = LocalPath(tmp_path / "dst.txt")
    target.write_text("PRECIOUS")
    with pytest.raises(OSError):
        src.copy(target, overwrite=True)
    assert target.read_text() == "PRECIOUS"


def test_copy_http_404_source_leaves_local_target_intact(http_server, tmp_path):
    pytest.importorskip("requests")
    from pathlib_next.uri import UriPath

    target = LocalPath(tmp_path / "cache.json")
    target.write_text("GOOD CACHE")
    with pytest.raises(FileNotFoundError):
        UriPath(http_server + "/no-such-file.json").copy(target, overwrite=True)
    assert target.read_text() == "GOOD CACHE"


def test_copy_failing_mid_stream_removes_partial_target(monkeypatch):
    root = MemPath("/")
    src = root / "src.bin"
    src.write_bytes(b"x" * 100)
    target = root / "dst.bin"

    original_open = MemPath.open

    class _Exploding:
        def __init__(self, inner):
            self._inner = inner
            self._reads = 0

        def read(self, size=-1):
            self._reads += 1
            if self._reads > 1:
                raise OSError("connection reset")
            return self._inner.read(10)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()

    def fake_open(self, mode="r", *args, **kwargs):
        handle = original_open(self, mode, *args, **kwargs)
        if self == src and "r" in mode:
            return _Exploding(handle)
        return handle

    monkeypatch.setattr(MemPath, "open", fake_open)
    with pytest.raises(OSError, match="connection reset"):
        src.copy(target)
    monkeypatch.undo()
    assert not target.exists()
    assert src.read_bytes() == b"x" * 100


@pytest.mark.parametrize("make_path", ["local", "mem"])
def test_copy_onto_itself_raises_and_keeps_content(tmp_path, make_path):
    if make_path == "local":
        path = LocalPath(tmp_path / "f.txt")
    else:
        path = MemPath("/") / "f.txt"
    path.write_text("DATA")
    with pytest.raises(OSError, match="same file"):
        path.copy(path, overwrite=True)
    assert path.read_text() == "DATA"


def test_copy_onto_case_alias_raises_on_case_insensitive_fs(tmp_path):
    path = LocalPath(tmp_path / "f.txt")
    path.write_text("DATA")
    alias = LocalPath(tmp_path / "F.TXT")
    if not alias.exists():
        pytest.skip("case-sensitive filesystem")
    with pytest.raises(OSError, match="same file"):
        path.copy(alias, overwrite=True)
    assert path.read_text() == "DATA"


def test_same_file_guard_ignores_equal_paths_on_different_mem_backends():
    # Equal segments on two separate in-memory filesystems are two files.
    a = MemPath("/") / "f.txt"
    b = MemPath("/") / "f.txt"
    a.write_text("A")
    b.write_text("B")
    a.copy(b, overwrite=True)
    assert b.read_text() == "A"
    assert a.read_text() == "A"


# --- move -----------------------------------------------------------------


def test_move_missing_source_does_not_remove_target_tree(tmp_path):
    target = LocalPath(tmp_path / "precious")
    target.mkdir()
    (target / "keep.txt").write_text("K")
    with pytest.raises(FileNotFoundError):
        LocalPath(tmp_path / "missing").move(target, overwrite=True)
    assert (target / "keep.txt").read_text() == "K"


def test_move_file_onto_directory_refuses_and_keeps_both(tmp_path):
    target = LocalPath(tmp_path / "precious")
    target.mkdir()
    (target / "keep.txt").write_text("K")
    src = LocalPath(tmp_path / "f.txt")
    src.write_text("F")
    with pytest.raises(IsADirectoryError):
        src.move(target, overwrite=True)
    assert (target / "keep.txt").read_text() == "K"
    assert src.read_text() == "F"


def test_move_file_over_file_replaces_it(tmp_path):
    src = LocalPath(tmp_path / "new.txt")
    src.write_text("NEW")
    target = LocalPath(tmp_path / "old.txt")
    target.write_text("OLD")
    src.move(target, overwrite=True)
    assert target.read_text() == "NEW"
    assert not src.exists()


def test_case_only_move_renames_instead_of_deleting(tmp_path):
    src = LocalPath(tmp_path / "readme.txt")
    src.write_text("R")
    target = LocalPath(tmp_path / "README.txt")
    if not target.exists():
        pytest.skip("case-sensitive filesystem")
    src.move(target, overwrite=True)
    assert os.listdir(tmp_path) == ["README.txt"]
    assert target.read_text() == "R"


def test_move_onto_itself_keeps_the_file(tmp_path):
    path = LocalPath(tmp_path / "x.txt")
    path.write_text("X")
    path.move(path, overwrite=True)
    assert path.read_text() == "X"


def test_mem_move_over_existing_file_replaces_it():
    root = MemPath("/")
    src = root / "a.txt"
    src.write_text("A")
    target = root / "b.txt"
    target.write_text("B")
    src.move(target, overwrite=True)
    assert target.read_text() == "A"
    assert not src.exists()


# --- rm through links -----------------------------------------------------


def _junction_or_skip(link, target):
    if sys.platform != "win32":
        pytest.skip("junctions are Windows-only")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not os.path.lexists(link):
        pytest.skip(f"could not create junction: {result.stderr.strip()}")


def _victim(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("V")
    return victim


def test_rm_recursive_does_not_descend_into_junction(tmp_path):
    victim = _victim(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    _junction_or_skip(tree / "j", victim)
    LocalPath(tree).rm(recursive=True)
    assert not tree.exists()
    assert (victim / "keep.txt").read_text() == "V"


def test_rm_recursive_on_junction_itself_removes_only_the_link(tmp_path):
    victim = _victim(tmp_path)
    link = tmp_path / "j"
    _junction_or_skip(link, victim)
    LocalPath(link).rm(recursive=True)
    assert not os.path.lexists(link)
    assert (victim / "keep.txt").read_text() == "V"


def test_file_uri_rm_recursive_does_not_descend_into_junction(tmp_path):
    pytest.importorskip("uritools")
    from pathlib_next.uri import UriPath

    victim = _victim(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    _junction_or_skip(tree / "j", victim)
    UriPath(LocalPath(tree)).rm(recursive=True)
    assert not tree.exists()
    assert (victim / "keep.txt").read_text() == "V"


def test_file_uri_rm_recursive_does_not_follow_directory_symlink(tmp_path):
    pytest.importorskip("uritools")
    from pathlib_next.uri import UriPath

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("P")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("a")
    try:
        os.symlink(outside, tree / "link", target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory symlink unavailable: {error}")

    UriPath(LocalPath(tree)).rm(recursive=True)

    assert (outside / "precious.txt").read_text() == "P"
    assert not os.path.lexists(tree / "link")


def test_file_uri_scandir_reports_symlink_not_directory(tmp_path):
    pytest.importorskip("uritools")
    from pathlib_next.uri import UriPath

    (tmp_path / "real").mkdir()
    try:
        os.symlink(tmp_path / "real", tmp_path / "link", target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory symlink unavailable: {error}")

    stats = dict(UriPath(LocalPath(tmp_path))._scandir())
    assert stats["link"].is_symlink()
    assert not stats["link"].is_dir()
    assert stats["real"].is_dir()


# --- untrusted child names ------------------------------------------------


@pytest.mark.parametrize(
    "name", ["", ".", "..", "a/b", "../x", "/abs", "nul\0byte", None, 3]
)
def test_unsafe_child_names_rejected_on_every_flavour(name):
    from pathlib_next.utils import is_safe_child_name

    assert not is_safe_child_name(name)
    assert not is_safe_child_name(name, windows=True)


@pytest.mark.parametrize(
    "name", ["D:evil.txt", "C:..", "a\\..\\b", "x:stream", ".. ", ". "]
)
def test_windows_only_unsafe_child_names(name):
    from pathlib_next.utils import is_safe_child_name

    assert is_safe_child_name(name)
    assert not is_safe_child_name(name, windows=True)


@pytest.mark.parametrize("name", ["a.txt", ".hidden", "12-00.log", "...x", "name."])
def test_ordinary_child_names_accepted(name):
    from pathlib_next.utils import is_safe_child_name

    assert is_safe_child_name(name)
    assert is_safe_child_name(name, windows=True)


def test_is_windows_flavoured_matches_path_semantics(tmp_path):
    from pathlib import PurePosixPath, PureWindowsPath

    from pathlib_next.utils import is_windows_flavoured

    assert is_windows_flavoured(PureWindowsPath("C:/x"))
    assert not is_windows_flavoured(PurePosixPath("/x"))
    assert not is_windows_flavoured(MemPath("/"))
    assert is_windows_flavoured(LocalPath(tmp_path)) == (os.name == "nt")
