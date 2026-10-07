"""What `copy()` and `move()` produce does not depend on how they got there.

A move that renames and a move that copies and deletes (another filesystem,
a backend without `rename()`) leave the same result, and a copy keeps the
permissions of directories as it keeps those of files.
"""

from __future__ import annotations

import errno
import os
import stat

import pytest

from pathlib_next import LocalPath
from pathlib_next.mempath import MemPath
from pathlib_next.utils.stat import FileStat

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


def _symlink_or_skip(link, target, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")


class AcrossDevices(LocalPath):
    """A local path whose `rename()` always reports another device."""

    __slots__ = ()

    def rename(self, target):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def replace(self, target):
        raise OSError(errno.EXDEV, "Invalid cross-device link")


# --- move: a link stays a link ------------------------------------------------


def test_move_across_devices_keeps_a_directory_link_a_link(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.txt").write_text("A")
    _symlink_or_skip(tmp_path / "link", tmp_path / "real", directory=True)
    (tmp_path / "out").mkdir()

    AcrossDevices(tmp_path / "link").move(AcrossDevices(tmp_path / "out" / "moved"))

    assert os.path.islink(tmp_path / "out" / "moved")
    assert not os.path.lexists(tmp_path / "link")
    assert (tmp_path / "real" / "a.txt").read_text() == "A"


def test_move_across_devices_keeps_a_file_link_a_link(tmp_path):
    (tmp_path / "real.txt").write_text("A")
    _symlink_or_skip(tmp_path / "link", tmp_path / "real.txt")
    text = os.readlink(tmp_path / "link")

    AcrossDevices(tmp_path / "link").move(AcrossDevices(tmp_path / "moved"))

    assert os.path.islink(tmp_path / "moved")
    assert os.readlink(tmp_path / "moved") == text
    assert not os.path.lexists(tmp_path / "link")
    assert (tmp_path / "real.txt").read_text() == "A"


def test_move_across_devices_keeps_the_links_inside_a_tree(tmp_path):
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "x.txt").write_text("X")
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "own.txt").write_text("O")
    _symlink_or_skip(tmp_path / "tree" / "link", tmp_path / "other", directory=True)

    AcrossDevices(tmp_path / "tree").move(AcrossDevices(tmp_path / "moved"))

    assert not os.path.lexists(tmp_path / "tree")
    assert os.path.islink(tmp_path / "moved" / "link")
    assert (tmp_path / "moved" / "own.txt").read_text() == "O"
    assert (tmp_path / "other" / "x.txt").read_text() == "X"


def test_move_gives_the_same_result_with_and_without_rename(tmp_path):
    (tmp_path / "real").mkdir()
    _symlink_or_skip(tmp_path / "a", tmp_path / "real", directory=True)
    _symlink_or_skip(tmp_path / "b", tmp_path / "real", directory=True)
    LocalPath(tmp_path / "a").move(LocalPath(tmp_path / "moved-a"))
    AcrossDevices(tmp_path / "b").move(AcrossDevices(tmp_path / "moved-b"))
    assert os.path.islink(tmp_path / "moved-a")
    assert os.path.islink(tmp_path / "moved-b")


def test_move_of_a_link_to_a_store_without_links_moves_the_content(tmp_path):
    (tmp_path / "real.txt").write_text("A")
    _symlink_or_skip(tmp_path / "link", tmp_path / "real.txt")
    target = MemPath("/moved.txt")

    LocalPath(tmp_path / "link").move(target)

    assert target.read_text() == "A"
    assert not os.path.lexists(tmp_path / "link")
    assert (tmp_path / "real.txt").read_text() == "A"


# --- move: rename() only onto the same class ----------------------------------


class RenamingMem(MemPath):
    """A memory path with a `rename()` of its own that records its targets."""

    __slots__ = ()
    renamed: list = []

    def rename(self, target):
        type(self).renamed.append(target)
        target.write_bytes(self.read_bytes())
        self.unlink()
        return target


def test_move_does_not_rename_onto_a_path_of_another_class(tmp_path):
    RenamingMem.renamed.clear()
    source = RenamingMem("/data.txt")
    source.write_text("DATA")
    target = LocalPath(tmp_path / "out.txt")

    source.move(target)

    assert RenamingMem.renamed == []
    assert target.read_text() == "DATA"
    assert not source.exists()


def test_move_renames_onto_a_path_of_the_same_class():
    RenamingMem.renamed.clear()
    source = RenamingMem("/data.txt")
    source.write_text("DATA")
    target = RenamingMem("/moved.txt", backend=source.backend)

    source.move(target)

    assert RenamingMem.renamed == [target]
    assert target.read_text() == "DATA"


# --- copy: directory permissions ------------------------------------------------


@posix_only
def test_copy_recursive_keeps_directory_permissions(tmp_path):
    src = tmp_path / "src"
    (src / "private").mkdir(parents=True)
    (src / "private" / "secret.txt").write_text("S")
    os.chmod(src / "private" / "secret.txt", 0o640)
    os.chmod(src / "private", 0o700)
    os.chmod(src, 0o750)

    LocalPath(src).copy(LocalPath(tmp_path / "dst"), recursive=True)

    def mode(path):
        return stat.S_IMODE(os.stat(path).st_mode)

    assert mode(tmp_path / "dst") == 0o750
    assert mode(tmp_path / "dst" / "private") == 0o700
    assert mode(tmp_path / "dst" / "private" / "secret.txt") == 0o640


@posix_only
def test_copy_recursive_of_a_read_only_directory_copies_its_children_first(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("F")
    os.chmod(src, 0o555)
    dst = tmp_path / "dst"
    try:
        LocalPath(src).copy(LocalPath(dst), recursive=True)
        assert (dst / "f.txt").read_text() == "F"
        assert stat.S_IMODE(os.stat(dst).st_mode) == 0o555
    finally:
        os.chmod(src, 0o755)
        if dst.exists():
            os.chmod(dst, 0o755)


@posix_only
def test_copy_recursive_without_preserve_metadata_leaves_directory_modes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    os.chmod(src, 0o700)
    old = os.umask(0o022)
    try:
        LocalPath(src).copy(
            LocalPath(tmp_path / "dst"), recursive=True, preserve_metadata=False
        )
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(tmp_path / "dst").st_mode) == 0o755


class Moded(MemPath):
    """A memory path that reports a Unix mode for every entry and records
    the modes it is given."""

    __slots__ = ()
    mode = stat.S_IFREG | 0o4755
    chmods: list = []

    def stat(self, *, follow_symlinks=True):
        real = super().stat(follow_symlinks=follow_symlinks)
        mode = self.mode if not real.is_dir() else stat.S_IFDIR | 0o1777
        return FileStat(st_mode=mode, st_size=real.st_size, is_dir=real.is_dir())

    def chmod(self, mode, *, follow_symlinks=True):
        type(self).chmods.append((self.name, mode))


class Other(Moded):
    """Another class with the same behaviour."""

    __slots__ = ()
    chmods: list = []


def test_copy_between_classes_drops_setuid_setgid_and_sticky_bits():
    Other.chmods.clear()
    source = Moded("/f.txt")
    source.write_text("F")
    target = Other("/g.txt", backend=source.backend)

    source.copy(target)

    assert Other.chmods == [("g.txt", 0o755)]


def test_copy_within_a_class_keeps_the_special_bits():
    Moded.chmods.clear()
    source = Moded("/f.txt")
    source.write_text("F")
    target = Moded("/g.txt", backend=source.backend)

    source.copy(target)

    assert Moded.chmods == [("g.txt", 0o4755)]


def test_copy_recursive_between_classes_drops_the_sticky_bit_of_a_directory():
    Other.chmods.clear()
    source = Moded("/d")
    source.mkdir()
    (source / "f.txt").write_text("F")
    target = Other("/e", backend=source.backend)

    source.copy(target, recursive=True)

    assert Other.chmods == [("f.txt", 0o755), ("e", 0o777)]


# --- copy: a dangling link at the target --------------------------------------


def test_copy_writes_through_a_dangling_symlink_as_shutil_does(tmp_path):
    (tmp_path / "src.txt").write_text("SRC")
    _symlink_or_skip(tmp_path / "dangling", tmp_path / "elsewhere.txt")

    LocalPath(tmp_path / "src.txt").copy(LocalPath(tmp_path / "dangling"))

    assert (tmp_path / "elsewhere.txt").read_text() == "SRC"
    assert os.path.islink(tmp_path / "dangling")


def test_copy_overwrite_replaces_a_live_symlink_at_the_target(tmp_path):
    (tmp_path / "src.txt").write_text("SRC")
    (tmp_path / "real.txt").write_text("REAL")
    _symlink_or_skip(tmp_path / "link", tmp_path / "real.txt")

    LocalPath(tmp_path / "src.txt").copy(LocalPath(tmp_path / "link"), overwrite=True)

    assert not os.path.islink(tmp_path / "link")
    assert (tmp_path / "link").read_text() == "SRC"
    assert (tmp_path / "real.txt").read_text() == "REAL"
