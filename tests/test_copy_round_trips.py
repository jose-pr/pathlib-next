"""How many `stat()` calls `copy()` and `move()` make on a backend whose stat
is a `FileStat`, and that fewer calls still refuse what they refused."""

from __future__ import annotations

import errno

import pytest

import pathlib_next

from pathlib_next.mempath import MemPath, MemPathBackend
from pathlib_next.utils.stat import FileStat


class Counting(MemPath):
    """A memory path that counts `stat()` calls and says nothing about where
    its node is, as a remote scheme does (the same-file test then falls to
    `_same_filesystem()` and `==`)."""

    __slots__ = ()
    stats = 0

    def stat(self, *, follow_symlinks=True):
        Counting.stats += 1
        return super().stat(follow_symlinks=follow_symlinks)

    def _node_key(self):
        return None


class Located(Counting):
    """The same, but answering `_node_key()` as `MemPath` does."""

    __slots__ = ()

    def _node_key(self):
        return MemPath._node_key(self)


@pytest.fixture(params=[Counting, Located], ids=lambda c: c.__name__)
def root(request):
    root = request.param("/", backend=MemPathBackend())
    (root / "src.txt").write_text("x")
    (root / "tree").mkdir()
    for index in range(10):
        (root / "tree" / f"f{index}.txt").write_text("x")
    Counting.stats = 0
    return root


def test_a_file_copy_stats_the_source_and_the_target_once(root):
    (root / "src.txt").copy(root / "dst.txt")
    assert Counting.stats == 2


def test_overwriting_a_file_costs_no_more(root):
    (root / "dst.txt").write_text("old")
    Counting.stats = 0
    (root / "src.txt").copy(root / "dst.txt", overwrite=True)
    assert Counting.stats == 2
    assert (root / "dst.txt").read_text() == "x"


def test_progress_reuses_the_size_of_the_source_stat(root):
    seen = []
    (root / "src.txt").copy(root / "dst.txt", progress=lambda *a: seen.append(a))
    assert Counting.stats == 2
    assert seen[-1][1:] == (1, 1)


def test_a_tree_copy_costs_two_stats_per_file(root):
    (root / "tree").copy(root / "tree2", recursive=True)
    assert Counting.stats <= 2 * 10 + 2
    assert sorted(p.name for p in (root / "tree2").iterdir()) == sorted(
        f"f{index}.txt" for index in range(10)
    )


def test_a_move_by_copy_stats_less_than_it_did(root):
    (root / "src.txt").move(root / "moved.txt")
    # Four where the class answers _node_key(); a class that does not is
    # also asked samefile(), which stats both sides before it gives up.
    assert Counting.stats <= (4 if isinstance(root, Located) else 6)
    assert (root / "moved.txt").read_text() == "x"
    assert not (root / "src.txt").exists()


def test_copy_still_refuses_a_file_onto_itself(root):
    with pytest.raises(OSError) as raised:
        (root / "src.txt").copy(root / "src.txt", overwrite=True)
    assert raised.value.errno == errno.EINVAL
    assert (root / "src.txt").read_text() == "x"


def test_copy_still_refuses_an_existing_target_and_a_directory(root):
    (root / "dst.txt").write_text("old")
    with pytest.raises(FileExistsError):
        (root / "src.txt").copy(root / "dst.txt")
    with pytest.raises(IsADirectoryError):
        (root / "src.txt").copy(root / "tree", overwrite=True)
    assert (root / "dst.txt").read_text() == "old"


def test_copy_still_leaves_the_target_alone_when_the_source_is_missing(root):
    (root / "dst.txt").write_text("old")
    with pytest.raises(FileNotFoundError):
        (root / "nope.txt").copy(root / "dst.txt", overwrite=True)
    assert (root / "dst.txt").read_text() == "old"


class IdentityStat(FileStat):
    __slots__ = ("st_dev", "st_ino")


class Aliased(Counting):
    """A backend whose stat carries an identity, and two names for one file."""

    __slots__ = ()

    def stat(self, *, follow_symlinks=True):
        plain = super().stat(follow_symlinks=follow_symlinks)
        result = IdentityStat()
        for field in (*FileStat._FIELDS, "mode_known"):
            setattr(result, field, getattr(plain, field))
        result.st_dev = 1
        result.st_ino = {"/a.txt": 7, "/alias.txt": 7}.get(self.as_posix(), id(self))
        return result


def test_copy_refuses_two_names_of_one_file_by_the_identity_in_the_stats():
    root = Aliased("/", backend=MemPathBackend())
    (root / "a.txt").write_text("keep")
    (root / "alias.txt").write_text("keep")
    with pytest.raises(OSError) as raised:
        (root / "a.txt").copy(root / "alias.txt", overwrite=True)
    assert raised.value.errno == errno.EINVAL
    assert (root / "alias.txt").read_text() == "keep"


def test_an_unknown_source_stat_falls_back_to_the_old_flow():
    class NoStat(Counting):
        __slots__ = ()

        def stat(self, *, follow_symlinks=True):
            if self.as_posix() == "/src.txt":
                raise NotImplementedError("stat")
            return super().stat(follow_symlinks=follow_symlinks)

    root = NoStat("/", backend=MemPathBackend())
    (root / "src.txt").write_text("x")
    (root / "src.txt").copy(root / "dst.txt", preserve_metadata=False)
    assert (root / "dst.txt").read_text() == "x"


def test_filestat_has_no_identity_to_compare():
    from pathlib_next.path import _same_identity

    assert _same_identity(FileStat(), FileStat()) is False


def test_localpath_copy_and_move_are_the_generic_ones():
    assert pathlib_next.LocalPath.copy is pathlib_next.Path.copy
    assert pathlib_next.LocalPath.move is pathlib_next.Path.move
    for name in ("copy", "move"):
        defined = vars(pathlib_next.LocalPath).get(name)
        assert defined is None or defined is getattr(pathlib_next.Path, name)


def test_the_junction_alias_and_the_unused_alias_are_gone():
    from pathlib_next import path

    assert not hasattr(path.Path, "_is_junction_link")
    assert not hasattr(path, "_FsPathLike")
