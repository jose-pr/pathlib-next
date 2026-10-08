"""`walk()` and a Windows junction: a second name for another tree, listed as
a file and never entered unless symlinks are followed, as `pathlib` does."""

from __future__ import annotations

import itertools
import os
import pathlib
import sys

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath, MemPathBackend


class WithJunction(MemPath):
    """A memory tree in which `/tree/junc` is a junction to `/outside`."""

    __slots__ = ()

    def is_junction(self):
        return self.as_posix() == "/tree/junc"

    def _scandir(self):
        if self.as_posix() == "/tree/junc":
            yield from MemPath("/outside", backend=self.backend)._scandir()
            return
        yield from super()._scandir()


def _memory_tree():
    backend = MemPathBackend()
    root = WithJunction("/", backend=backend)
    (root / "tree").mkdir()
    (root / "tree" / "sub").mkdir()
    (root / "tree" / "sub" / "f.txt").write_text("f")
    (root / "outside").mkdir()
    (root / "outside" / "o.txt").write_text("o")
    (root / "tree" / "junc").mkdir()
    return root


def _shape(walked):
    return sorted((p.as_posix(), sorted(d), sorted(f)) for p, d, f in walked)


def test_a_junction_is_listed_as_a_file_and_not_entered():
    root = _memory_tree()
    assert _shape(root.joinpath("tree").walk()) == [
        ("/tree", ["sub"], ["junc"]),
        ("/tree/sub", [], ["f.txt"]),
    ]


def test_a_junction_is_listed_as_a_file_bottom_up_too():
    root = _memory_tree()
    assert _shape(root.joinpath("tree").walk(top_down=False)) == [
        ("/tree", ["sub"], ["junc"]),
        ("/tree/sub", [], ["f.txt"]),
    ]


def test_following_symlinks_enters_a_junction():
    root = _memory_tree()
    assert _shape(root.joinpath("tree").walk(follow_symlinks=True)) == [
        ("/tree", ["junc", "sub"], []),
        ("/tree/junc", [], ["o.txt"]),
        ("/tree/sub", [], ["f.txt"]),
    ]


def test_the_top_of_a_walk_may_itself_be_a_junction():
    root = _memory_tree()
    assert _shape(root.joinpath("tree", "junc").walk()) == [
        ("/tree/junc", [], ["o.txt"])
    ]


def test_a_class_that_cannot_answer_is_walked_as_before():
    class Unsure(WithJunction):
        __slots__ = ()

        def is_junction(self):
            raise NotImplementedError("is_junction")

    root = Unsure("/", backend=_memory_tree().backend)
    assert _shape(root.joinpath("tree").walk()) == [
        ("/tree", ["junc", "sub"], []),
        ("/tree/junc", [], ["o.txt"]),
        ("/tree/sub", [], ["f.txt"]),
    ]


needs_windows = pytest.mark.skipif(os.name != "nt", reason="junctions are Windows")


def _junction(link: pathlib.Path, target: pathlib.Path):
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _local_tree(tmp_path):
    (tmp_path / "tree" / "sub").mkdir(parents=True)
    (tmp_path / "tree" / "sub" / "f.txt").write_text("f")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "o.txt").write_text("o")
    _junction(tmp_path / "tree" / "junc", tmp_path / "outside")
    return tmp_path / "tree"


def _relative(walked, base):
    return sorted(
        (os.path.relpath(p, base).replace(os.sep, "/"), sorted(d), sorted(f))
        for p, d, f in walked
    )


@needs_windows
@pytest.mark.parametrize("keywords", [{}, {"top_down": False}])
def test_a_real_junction_is_listed_as_a_file_and_not_entered(tmp_path, keywords):
    tree = _local_tree(tmp_path)
    walked = _relative(pathlib_next.LocalPath(tree).walk(**keywords), tmp_path)
    assert walked == [
        ("tree", ["sub"], ["junc"]),
        ("tree/sub", [], ["f.txt"]),
    ]


@needs_windows
def test_a_real_junction_is_entered_when_following_symlinks(tmp_path):
    tree = _local_tree(tmp_path)
    walked = _relative(
        pathlib_next.LocalPath(tree).walk(follow_symlinks=True), tmp_path
    )
    assert ("tree/junc", [], ["o.txt"]) in walked


@needs_windows
def test_a_junction_to_its_own_parent_is_not_walked_into(tmp_path):
    (tmp_path / "loop").mkdir()
    (tmp_path / "loop" / "x.txt").write_text("x")
    _junction(tmp_path / "loop" / "again", tmp_path / "loop")
    errors = []
    walked = list(
        itertools.islice(
            pathlib_next.LocalPath(tmp_path / "loop").walk(on_error=errors.append), 50
        )
    )
    assert len(walked) == 1
    assert errors == []
    assert sorted(walked[0][2]) == ["again", "x.txt"]


@needs_windows
@pytest.mark.skipif(
    not hasattr(os, "_walk_symlinks_as_files"),
    reason="Path.walk lists a junction as a file from 3.14",
)
@pytest.mark.parametrize(
    "keywords", [{}, {"top_down": False}, {"follow_symlinks": True}]
)
def test_a_walk_over_a_junction_equals_pathlibs(tmp_path, keywords):
    tree = _local_tree(tmp_path)
    ours = _relative(pathlib_next.LocalPath(tree).walk(**keywords), tmp_path)
    theirs = _relative(pathlib.Path(tree).walk(**keywords), tmp_path)
    assert ours == theirs
