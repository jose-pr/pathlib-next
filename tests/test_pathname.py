"""Pure-path operations exercised across the three Pathname implementations
that route through Pathname's generic (non-pathlib-inherited) code: a plain
Pathname (PosixPathname -- LocalPath itself is excluded here since pathlib's
own PurePath wins those methods via MRO, see test_parity_pure.py instead),
Uri, and MemPath.
"""

import pathlib
import sys

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
    try:
        expected = pathlib.PurePosixPath("a/b.txt").with_suffix(".").name
    except ValueError:
        with pytest.raises(ValueError):
            p.with_suffix(".")
    else:
        assert p.with_suffix(".").name == expected


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

    # Stdlib's own `parents` (which PosixPathname inherits) gained slices and
    # negative indices in 3.10. Only that combination may refuse them: a
    # blanket try/except also hid a regression in the generic
    # `_PathnameParents` used by Uri and MemPath.
    stdlib_39_parents = cls is PosixPathname and sys.version_info < (3, 10)

    # Slicing
    if stdlib_39_parents:
        with pytest.raises(TypeError):
            p.parents[0:2]
    else:
        sliced = p.parents[0:2]
        assert [x.as_posix() for x in sliced] == ["a/b", "a"]

    # Negative indexing
    if stdlib_39_parents:
        with pytest.raises(IndexError):
            p.parents[-1]
    else:
        assert p.parents[-1].as_posix() in ("", ".")
        assert p.parents[-2].as_posix() == "a"
        assert p.parents[-3].as_posix() == "a/b"

    # IndexError out of bounds
    with pytest.raises(IndexError):
        _ = p.parents[3]
    with pytest.raises(IndexError):
        _ = p.parents[-4]


@pytest.mark.parametrize("cls", IMPLS)
def test_parents_of_a_rooted_path_end_at_the_root(cls):
    p = cls("/a/b")
    assert [pp.as_posix() for pp in p.parents] == ["/a", "/"]
    assert len(p.parents) == 2
    assert p.parent.parent.as_posix() == "/"
    assert p.is_relative_to("/")
    assert p.is_relative_to(cls("/"))


@pytest.mark.parametrize("cls", IMPLS)
@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("a/b/c.py", "b/*.py", True),
        ("a/b/c.py", "a/*.py", False),
        ("/a/b", "a/b", True),
        ("/a/b", "/a/b", True),
        ("a/b", "/a/b", False),
        ("a/b/c.py", "/b/*.py", False),
    ],
)
def test_match_is_right_anchored_per_segment(cls, path, pattern, expected):
    # PosixPathname resolves stdlib's match (MRO); Uri and MemPath the
    # generic Pathname.match -- both routes must agree with pathlib.
    assert pathlib.PurePosixPath(path).match(pattern) is expected
    assert cls(path).match(pattern) is expected


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


# --- 2026-08-16: equality + is_relative_to str/object agreement ---


@pytest.mark.parametrize("cls", IMPLS)
def test_equality_is_by_value_not_identity(cls):
    # `Pathname` used to define no __eq__ at all, so any subclass that
    # didn't hand-write one (MemPath, and every downstream Track A class)
    # compared by identity.
    assert cls("a/b") == cls("a/b")
    assert cls("a/b") != cls("a/c")
    assert cls("a/b") is not cls("a/b")


@pytest.mark.parametrize("cls", IMPLS)
def test_hash_matches_equality(cls):
    assert hash(cls("a/b")) == hash(cls("a/b"))
    assert len({cls("a/b"), cls("a/b"), cls("a/c")}) == 2
    assert {cls("a/b"): 1}[cls("a/b")] == 1


@pytest.mark.parametrize("cls", IMPLS)
def test_equality_against_a_foreign_type_is_false_not_an_error(cls):
    assert (cls("a/b") == object()) is False
    assert (cls("a/b") != object()) is True


@pytest.mark.parametrize("cls", IMPLS)
@pytest.mark.parametrize("path,other", [("a/b", "a"), ("a/b/c", "a/b"), ("a", "a")])
def test_is_relative_to_str_and_object_agree_with_stdlib(cls, path, other):
    expected = pathlib.PurePosixPath(path).is_relative_to(other)
    assert expected is True  # sanity: the oracle really does say True
    assert cls(path).is_relative_to(cls(other)) is expected
    # The str form used to join `other` onto `self` instead of parsing it
    # standalone, so it answered False where the object form answered True.
    assert cls(path).is_relative_to(other) is expected


@pytest.mark.parametrize("cls", IMPLS)
@pytest.mark.parametrize("path,other", [("a/b", "b"), ("a/b", "c"), ("ab/c", "a")])
def test_is_relative_to_negative_str_and_object_agree_with_stdlib(cls, path, other):
    expected = pathlib.PurePosixPath(path).is_relative_to(other)
    assert expected is False
    assert cls(path).is_relative_to(cls(other)) is expected
    assert cls(path).is_relative_to(other) is expected


def test_mempath_is_relative_to_str_keeps_the_backend():
    # with_segments(), not type(self)(other): the bare constructor hands
    # MemPath a fresh empty backend, which is the kind of per-instance
    # state a generic normalization must not drop.
    from pathlib_next.mempath import MemPathBackend

    backend = MemPathBackend()
    p = MemPath("a/b", backend=backend)
    assert p.is_relative_to("a") is True
    assert p.with_segments("a").backend is backend


def test_generic_pathname_subclass_gets_working_equality():
    """A minimal Track A subclass -- the shape `docs/guides/extending.md`
    documents -- must get equality (and therefore is_relative_to) without
    hand-writing __eq__."""
    from pathlib_next.path import Pathname

    class Toy(Pathname):
        __slots__ = ("_segments",)

        def __init__(self, *segments):
            parts = []
            for segment in segments:
                if isinstance(segment, Pathname):
                    parts.extend(segment.segments)
                else:
                    parts.append(segment)
            self._segments = "/".join(parts).split("/")

        @property
        def segments(self):
            return self._segments

        @property
        def parts(self):
            return tuple(self._segments)

        @property
        def parent(self):
            return self.with_segments(*self._segments[:-1])

        def with_segments(self, *segments):
            return type(self)(*segments)

        def relative_to(self, other):
            raise NotImplementedError()

        def as_uri(self):
            return "toy:" + self.as_posix()

    assert Toy("a", "b") == Toy("a/b")
    assert Toy("a/b") != Toy("a/c")
    assert len({Toy("a/b"), Toy("a/b")}) == 1
    assert Toy("a/b").is_relative_to(Toy("a")) is True
    assert Toy("a/b").is_relative_to("a") is True
    assert Toy("a/b").is_relative_to("c") is False


def test_localpath_keeps_stdlib_equality_semantics():
    """`pathlib.PurePath` precedes `Pathname` in the fspath MRO, so the new
    default must NOT displace stdlib's (case-folding, cross-subclass)
    equality there."""
    from pathlib_next.fspath import LocalPath, _BaseFSPathname
    from pathlib_next.path import Pathname

    for cls in (LocalPath, PosixPathname):
        names = [k.__name__ for k in cls.__mro__]
        assert names.index("PurePath") < names.index("Pathname")
        assert cls.__eq__ is not Pathname.__eq__
        assert cls.__eq__ is pathlib.PurePath.__eq__

    assert issubclass(PosixPathname, _BaseFSPathname)
    assert LocalPath("a/b") == pathlib.Path("a/b")
