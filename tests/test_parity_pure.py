"""Same inputs fed to PosixPathname and pathlib.PurePosixPath, asserting
identical outputs. Using stdlib PurePosixPath as the oracle keeps this
correct across Python versions without hand-maintaining expected values.
"""
import pathlib

import pytest

from pathlib_next.fspath import PosixPathname

PATHS = [
    "a/b/c",
    "a/b/c.txt",
    "a/b/c.tar.gz",
    "/a/b/c",
    "a",
    ".hidden",
    "a/.hidden",
    "a//b",
    "a/./b",
]


@pytest.mark.parametrize("path", PATHS)
def test_name_stem_suffix_suffixes(path):
    ours = PosixPathname(path)
    theirs = pathlib.PurePosixPath(path)
    assert ours.name == theirs.name
    assert ours.stem == theirs.stem
    assert ours.suffix == theirs.suffix
    assert ours.suffixes == theirs.suffixes


@pytest.mark.parametrize("path", PATHS)
def test_parent_and_parents(path):
    ours = PosixPathname(path)
    theirs = pathlib.PurePosixPath(path)
    assert ours.parent.as_posix() == theirs.parent.as_posix()
    assert [p.as_posix() for p in ours.parents] == [
        p.as_posix() for p in theirs.parents
    ]


@pytest.mark.parametrize("path", PATHS)
def test_as_posix(path):
    assert PosixPathname(path).as_posix() == pathlib.PurePosixPath(path).as_posix()


@pytest.mark.parametrize("path", PATHS)
def test_is_absolute(path):
    assert PosixPathname(path).is_absolute() == pathlib.PurePosixPath(
        path
    ).is_absolute()


@pytest.mark.parametrize(
    "path,name", [("a/b/c.txt", "d.py"), ("a/b/c", "d")]
)
def test_with_name(path, name):
    ours = PosixPathname(path).with_name(name)
    theirs = pathlib.PurePosixPath(path).with_name(name)
    assert ours.as_posix() == theirs.as_posix()


@pytest.mark.parametrize(
    "path,suffix", [("a/b/c.txt", ".py"), ("a/b/c", ".py"), ("a/b/c.txt", "")]
)
def test_with_suffix(path, suffix):
    ours = PosixPathname(path).with_suffix(suffix)
    theirs = pathlib.PurePosixPath(path).with_suffix(suffix)
    assert ours.as_posix() == theirs.as_posix()


@pytest.mark.parametrize("path", PATHS)
def test_joinpath(path):
    ours = PosixPathname(path).joinpath("x", "y")
    theirs = pathlib.PurePosixPath(path).joinpath("x", "y")
    assert ours.as_posix() == theirs.as_posix()


@pytest.mark.parametrize(
    "path,other",
    [("a/b/c", "a/b"), ("a/b/c", "a"), ("a/b/c", "x")],
)
def test_is_relative_to_and_relative_to(path, other):
    ours = PosixPathname(path)
    theirs = pathlib.PurePosixPath(path)
    ours_other = PosixPathname(other)
    theirs_other = pathlib.PurePosixPath(other)
    assert ours.is_relative_to(ours_other) == theirs.is_relative_to(theirs_other)
    if theirs.is_relative_to(theirs_other):
        assert ours.relative_to(ours_other).as_posix() == theirs.relative_to(
            theirs_other
        ).as_posix()


@pytest.mark.parametrize("pattern", ["*.txt", "c*", "a/*/c.txt", "*"])
def test_match(pattern):
    path = "a/b/c.txt"
    ours = PosixPathname(path).match(pattern)
    theirs = pathlib.PurePosixPath(path).match(pattern)
    assert ours == theirs
