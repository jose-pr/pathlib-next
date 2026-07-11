"""Pure-path operations exercised across the three Pathname implementations
that route through Pathname's generic (non-pathlib-inherited) code: a plain
Pathname (PosixPathname -- LocalPath itself is excluded here since pathlib's
own PurePath wins those methods via MRO, see test_parity_pure.py instead),
Uri, and MemPath.
"""
import pytest

from pathlib_next.fspath import PosixPathname
from pathlib_next.mempath import MemPath
from pathlib_next.uri import Uri

IMPLS = [PosixPathname, Uri, MemPath]


@pytest.mark.parametrize("cls", IMPLS)
def test_name_suffix_stem(cls):
    p = cls("a/b/c.tar.gz")
    assert p.name == "c.tar.gz"
    assert p.suffix == ".gz"
    assert p.suffixes == [".tar", ".gz"]
    assert p.stem == "c.tar"


@pytest.mark.parametrize("cls", IMPLS)
def test_name_no_suffix(cls):
    p = cls("a/b/c")
    assert p.name == "c"
    assert p.suffix == ""
    assert p.suffixes == []
    assert p.stem == "c"


@pytest.mark.parametrize("cls", IMPLS)
def test_with_name(cls):
    p = cls("a/b/c.txt")
    assert p.with_name("d.py").name == "d.py"
    assert p.with_name("d.py").parent.name == "b"


@pytest.mark.parametrize("cls", IMPLS)
def test_with_name_empty_name_raises(cls):
    p = cls("")
    with pytest.raises(ValueError):
        p.with_name("x")


@pytest.mark.parametrize("cls", IMPLS)
def test_with_suffix(cls):
    p = cls("a/b.txt")
    assert p.with_suffix(".py").name == "b.py"
    assert p.with_suffix("").name == "b"


@pytest.mark.parametrize("cls", IMPLS)
def test_with_suffix_invalid_raises(cls):
    p = cls("a/b.txt")
    with pytest.raises(ValueError):
        p.with_suffix("txt")
    with pytest.raises(ValueError):
        p.with_suffix(".")


@pytest.mark.parametrize("cls", IMPLS)
def test_with_stem(cls):
    p = cls("a/b.txt")
    assert p.with_stem("c").name == "c.txt"


@pytest.mark.parametrize("cls", IMPLS)
def test_joinpath(cls):
    p = cls("a/b")
    joined = p.joinpath("c", "d")
    assert joined.as_posix() == "a/b/c/d"


@pytest.mark.parametrize("cls", IMPLS)
def test_truediv(cls):
    p = cls("a/b")
    assert (p / "c").as_posix() == "a/b/c"


@pytest.mark.parametrize("cls", IMPLS)
def test_parent_and_parents(cls):
    p = cls("a/b/c")
    assert p.parent.as_posix() == "a/b"
    assert p.parent.parent.as_posix() == "a"
    parents = [pp.as_posix() for pp in p.parents]
    assert parents[:2] == ["a/b", "a"]
    assert len(parents) == 3  # trailing root/"." element, like pathlib
    
    # Slicing
    try:
        sliced = p.parents[0:2]
        assert [x.as_posix() for x in sliced] == ["a/b", "a"]
    except TypeError:
        # Python 3.9 stdlib pathlib.PurePath.parents doesn't support slicing
        pass
    
    # Negative indexing
    try:
        assert p.parents[-1].as_posix() == "" or p.parents[-1].as_posix() == "."
        assert p.parents[-2].as_posix() == "a"
        assert p.parents[-3].as_posix() == "a/b"
    except IndexError:
        # Python 3.9 stdlib pathlib.PurePath.parents doesn't support negative indexing
        pass
    
    # IndexError out of bounds
    with pytest.raises(IndexError):
        _ = p.parents[3]
    with pytest.raises(IndexError):
        _ = p.parents[-4]



@pytest.mark.parametrize("cls", IMPLS)
def test_has_glob_pattern(cls):
    assert cls("a/*.py").has_glob_pattern()
    assert cls("a/foo*").has_glob_pattern()  # B7 regression: "foo*" (not anchored)
    assert not cls("a/b/c.py").has_glob_pattern()


@pytest.mark.parametrize("cls", IMPLS)
def test_match_b6_b7(cls):
    # B6: reversed isinstance() args used to crash Uri/MemPath's match().
    # B7: WILCARD_PATTERN.match (anchored) missed "foo*"-style trailing
    # wildcards during has_glob_pattern's internal use.
    p = cls("dir/foo.txt")
    assert p.match("*.txt")
    assert not p.match("*.py")


@pytest.mark.parametrize("cls", IMPLS)
@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("a/b/c.txt", True),
        ("a/*/c.txt", True),
        ("a/**/c.txt", True),
        ("z/*/c.txt", False),
        ("a/b", False),
    ],
)
def test_full_match(cls, pattern, expected):
    p = cls("a/b/c.txt")
    assert p.full_match(pattern) is expected


@pytest.mark.parametrize("cls", IMPLS)
def test_full_match_double_star_multi_segment(cls):
    assert cls("a/x/y/c.txt").full_match("a/**/c.txt")
    assert cls("a/c.txt").full_match("a/**/c.txt")  # ** matches zero segments


@pytest.mark.parametrize("cls", IMPLS)
def test_root_drive_anchor_relative(cls):
    p = cls("a/b")
    assert p.root == ""
    assert p.anchor == ""
