"""Property-based tests (hypothesis) for URI parsing/joining and parity
with `pathlib.PurePosixPath` on the operations that are supposed to behave
identically (see `docs/divergences.md` for the ones that deliberately
don't -- those are out of scope here, not asserted against).
"""
from __future__ import annotations

import pathlib

from hypothesis import given, settings
from hypothesis import strategies as st

from pathlib_next.uri import Uri

# A "safe" path segment: letters/digits/-/_ only, never empty, never "."
# or ".." -- keeps every property below unambiguous without wading into
# the documented "we never resolve .." / dot-collapsing divergences.
segment = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"
    ),
    min_size=1,
    max_size=12,
).filter(lambda s: s not in (".", ".."))

segments_list = st.lists(segment, min_size=0, max_size=6)
nonempty_segments_list = st.lists(segment, min_size=1, max_size=6)


def _posix(segs: list[str]) -> str:
    """Absolute posix path string for a list of segments."""
    return "/" + "/".join(segs)


# --- Parse/Format identity ---


@given(segs=segments_list)
@settings(max_examples=200)
def test_uri_str_roundtrip(segs):
    u = Uri(_posix(segs))
    assert Uri(str(u)) == u


@given(segs=segments_list)
@settings(max_examples=200)
def test_uri_as_uri_roundtrip(segs):
    u = Uri(_posix(segs))
    assert Uri(u.as_uri()) == u


# --- Path joining ---


@given(base=segments_list, a=segment, b=segment)
@settings(max_examples=200)
def test_join_is_associative(base, a, b):
    u = Uri(_posix(base))
    assert (u / a) / b == u.joinpath(a, b)


@given(base=segments_list, tail=nonempty_segments_list)
@settings(max_examples=200)
def test_joinpath_matches_repeated_truediv(base, tail):
    u = Uri(_posix(base))
    joined = u.joinpath(*tail)
    stepped = u
    for part in tail:
        stepped = stepped / part
    assert joined == stepped


# --- Parity vs pathlib.PurePosixPath ---


@given(segs=nonempty_segments_list)
@settings(max_examples=200)
def test_segments_match_pure_posix_path_parts(segs):
    path_str = _posix(segs)
    u = Uri(path_str)
    pp = pathlib.PurePosixPath(path_str)
    # Uri.segments keeps a leading "" to mark the root ("/a/b" -> ("", "a",
    # "b")); PurePosixPath.parts marks it as "/" instead ("/a/b" -> ("/",
    # "a", "b")). Everything after that first element matches exactly.
    assert u.segments[0] == ""
    assert u.segments[1:] == pp.parts[1:]


@given(segs=nonempty_segments_list)
@settings(max_examples=200)
def test_name_matches_pure_posix_path(segs):
    path_str = _posix(segs)
    assert Uri(path_str).name == pathlib.PurePosixPath(path_str).name


@given(common=segments_list, tail=nonempty_segments_list)
@settings(max_examples=200)
def test_relative_to_matches_pure_posix_path(common, tail):
    child_segs = common + tail
    child_str = _posix(child_segs)
    base_str = _posix(common)

    u_child, u_base = Uri(child_str), Uri(base_str)
    pp_child, pp_base = pathlib.PurePosixPath(child_str), pathlib.PurePosixPath(base_str)

    u_rel = u_child.relative_to(u_base)
    pp_rel = pp_child.relative_to(pp_base)
    assert u_rel.segments == pp_rel.parts


@given(a=nonempty_segments_list, b=nonempty_segments_list)
@settings(max_examples=200)
def test_relative_to_unrelated_raises_on_both(a, b):
    # Force genuinely unrelated paths (b must not start with a, and vice
    # versa) by prefixing each with a distinct marker segment.
    a_str = _posix(["left-marker"] + a)
    b_str = _posix(["right-marker"] + b)

    u_raises = False
    try:
        Uri(a_str).relative_to(Uri(b_str))
    except ValueError:
        u_raises = True

    pp_raises = False
    try:
        pathlib.PurePosixPath(a_str).relative_to(pathlib.PurePosixPath(b_str))
    except ValueError:
        pp_raises = True

    assert u_raises == pp_raises == True


@given(segs=nonempty_segments_list)
@settings(max_examples=200)
def test_match_trailing_segment_wildcard_matches_pure_posix_path(segs):
    path_str = _posix(segs)
    pattern = "*" + segs[-1][1:] if len(segs[-1]) > 1 else "*"
    assert Uri(path_str).match(pattern) == pathlib.PurePosixPath(path_str).match(pattern)


@given(segs=nonempty_segments_list)
@settings(max_examples=200)
def test_is_relative_to_matches_pure_posix_path(segs):
    path_str = _posix(segs)
    parent_str = _posix(segs[:-1])
    u, pp = Uri(path_str), pathlib.PurePosixPath(path_str)
    u_parent, pp_parent = Uri(parent_str), pathlib.PurePosixPath(parent_str)
    assert u.is_relative_to(u_parent) == pp.is_relative_to(pp_parent) == True
