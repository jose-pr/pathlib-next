"""Property-based tests (hypothesis) for URI parsing/joining and parity
with `pathlib.PurePosixPath` on the operations that are supposed to behave
identically (see `docs/divergences.md` for the ones that deliberately
don't -- those are out of scope here, not asserted against).
"""

from __future__ import annotations

import ipaddress
import pathlib
import sys

import uritools
from hypothesis import given, settings
from hypothesis import strategies as st

from pathlib_next.uri import Uri
from pathlib_next.uri.source import (
    Source,
    _compose_uri,
    _decode_host,
    _remove_dot_segments,
    _split_authority,
)

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
    pp_child, pp_base = pathlib.PurePosixPath(child_str), pathlib.PurePosixPath(
        base_str
    )

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
    assert Uri(path_str).match(pattern) == pathlib.PurePosixPath(path_str).match(
        pattern
    )


@given(segs=nonempty_segments_list)
@settings(max_examples=200)
def test_is_relative_to_matches_pure_posix_path(segs):
    path_str = _posix(segs)
    parent_str = _posix(segs[:-1])
    u, pp = Uri(path_str), pathlib.PurePosixPath(path_str)
    u_parent, pp_parent = Uri(parent_str), pathlib.PurePosixPath(parent_str)
    assert u.is_relative_to(u_parent) == pp.is_relative_to(pp_parent) == True
    # The str form must agree with the object form: it used to anchor
    # `other` at self's root (Uri(self, _ROOT, other)) and answer False for
    # every *relative* self.
    assert u.is_relative_to(parent_str) == pp.is_relative_to(parent_str) == True


@given(segs=nonempty_segments_list)
@settings(max_examples=200)
def test_is_relative_to_str_matches_pure_posix_path_for_unrelated_other(segs):
    # The negative direction of the same oracle: a str `other` that is not a
    # prefix must answer False for both, relative and absolute self alike.
    other_str = _posix(["zzz-unrelated"])
    for path_str in (_posix(segs), "/" + _posix(segs)):
        u, pp = Uri(path_str), pathlib.PurePosixPath(path_str)
        assert u.is_relative_to(other_str) == pp.is_relative_to(other_str)


# --- Generic Pathname (Uri, MemPath) vs PurePosixPath: match, parents,
# MemPath normalization. Small alphabets so wildcards actually hit. ---

_match_name = st.sampled_from(["a", "b", "ab", "a.py", ".a", "b.a"])
_match_pattern_part = st.sampled_from(
    ["a", "b", "*", "?", "a*", "*.py", "[ab]", "[!a]*", "**", ".*", "."]
)


def _generic_classes():
    from pathlib_next.mempath import MemPath

    return (Uri, MemPath)


@given(
    rooted=st.booleans(),
    names=st.lists(_match_name, min_size=0, max_size=4),
    pattern_rooted=st.booleans(),
    pattern_parts=st.lists(_match_pattern_part, min_size=0, max_size=4),
    case_sensitive=st.sampled_from([None, True, False]),
)
@settings(max_examples=400)
def test_generic_match_matches_pure_posix_path(
    rooted, names, pattern_rooted, pattern_parts, case_sensitive
):
    """Multi-segment and rooted patterns (not just a trailing "*"): right
    anchoring, `*` never crossing "/", anchors, and the empty pattern."""
    path_str = ("/" if rooted else "") + "/".join(names)
    pattern = ("/" if pattern_rooted else "") + "/".join(pattern_parts)
    kwargs = {} if case_sensitive is None else {"case_sensitive": case_sensitive}

    def outcome(p):
        try:
            if isinstance(p, pathlib.PurePath) and sys.version_info < (3, 12):
                # 3.9-3.11 stdlib has no case_sensitive=; posix is sensitive.
                if case_sensitive is False:
                    return pathlib.PurePosixPath(str(p).lower()).match(pattern.lower())
                return p.match(pattern)
            return p.match(pattern, **kwargs)
        except ValueError:
            return ValueError

    expected = outcome(pathlib.PurePosixPath(path_str))
    for cls in _generic_classes():
        assert outcome(cls(path_str)) == expected, (cls, path_str, pattern)


@given(rooted=st.booleans(), segs=segments_list)
@settings(max_examples=200)
def test_generic_parents_and_parent_match_pure_posix_path(rooted, segs):
    path_str = ("/" if rooted else "") + "/".join(segs)
    pp = pathlib.PurePosixPath(path_str)

    def spell(p):
        s = p.as_posix()
        return "" if s == "." else s

    for cls in _generic_classes():
        p = cls(path_str)
        assert [spell(x) for x in p.parents] == [spell(x) for x in pp.parents]
        assert spell(p.parent) == spell(pp.parent)
        for ancestor in pp.parents:
            assert p.is_relative_to(spell(ancestor)) is True


_mem_piece = st.sampled_from(["", ".", "a", "b", "cd"])


@given(
    args=st.lists(
        st.tuples(st.booleans(), st.lists(_mem_piece, max_size=4)),
        min_size=1,
        max_size=3,
    )
)
@settings(max_examples=300)
def test_mempath_normalizes_like_pure_posix_path(args):
    from pathlib_next.mempath import MemPath

    # POSIX keeps exactly two leading slashes as a distinct root; MemPath has
    # one root, so such arguments are not generated.
    strs = [("/" if rooted else "") + "/".join(pieces) for rooted, pieces in args]
    strs = [s for s in strs if not (s.startswith("//") and not s.startswith("///"))]
    if not strs:
        return
    pp = pathlib.PurePosixPath(*strs)
    ours = MemPath(*strs)
    assert str(ours) == ("" if str(pp) == "." else str(pp))
    assert ours == MemPath(str(pp)) or str(pp) == "."
    assert hash(ours) == hash(MemPath(ours.as_posix()))
    assert ours.name == pp.name
    assert ours.root == pp.root


# --- uritools oracle: one-pass parse/compose fast paths ---
# `Uri._parse_uri`/`Uri._format_parsed_parts` bypass uritools' get*()
# accessors and uricompose() for speed (one-pass authority/path extraction,
# direct string assembly) but must remain byte-for-byte equivalent to them
# -- uritools stays installed as the oracle here specifically so that
# equivalence stays enforced, not just true at the moment the bypass was
# written. A mismatch means the fast path is wrong, not these tests.

_scheme_st = st.one_of(
    st.none(), st.from_regex(r"[a-zA-Z][a-zA-Z0-9+.-]{0,8}", fullmatch=True)
)
_userinfo_st = st.one_of(
    st.none(), st.text(alphabet="abcXYZ012:%40@!$&'()*+,;=~-._", max_size=12)
)
_host_st = st.one_of(
    st.none(),
    st.just(""),
    st.text(alphabet="abcXYZ012.-%~", max_size=15),
    st.just("[::1]"),
    st.just("[2001:db8::1]"),
    st.just("[::1"),  # malformed bracket -- must raise on both sides
    st.text(alphabet="0123456789", max_size=6),  # pure-digit host (uritools quirk)
)
_port_st = st.one_of(st.none(), st.integers(min_value=0, max_value=99999).map(str))
_path_st = st.text(alphabet="abcXYZ012/.%20~!$&'()*+,;=:@-_", max_size=25)
_query_st = st.one_of(st.none(), st.text(alphabet="abcXYZ012=&%20+", max_size=15))
_frag_st = st.one_of(st.none(), st.text(alphabet="abcXYZ012%20-_", max_size=10))


@st.composite
def _uri_string(draw):
    scheme = draw(_scheme_st)
    userinfo = draw(_userinfo_st)
    host = draw(_host_st)
    port = draw(_port_st)
    path = draw(_path_st)
    query = draw(_query_st)
    frag = draw(_frag_st)

    parts = []
    if scheme is not None:
        parts.append(scheme + ":")
    has_authority = host is not None
    if has_authority:
        parts.append("//")
        if userinfo is not None:
            parts.append(userinfo + "@")
        parts.append(host)
        if port is not None:
            parts.append(":" + port)
    if path:
        if has_authority and not path.startswith("/"):
            path = "/" + path
        parts.append(path)
    if query is not None:
        parts.append("?" + query)
    if frag is not None:
        parts.append("#" + frag)
    return "".join(parts)


# The oracle is uritools' getters, with three deliberate contract changes
# (wave 5, Design Q1 and the non-UTF-8/data: findings):
# * the query is the RAW, still-encoded component, never decoded at parse;
# * percent-escapes that are not UTF-8 decode with surrogateescape instead
#   of raising UnicodeDecodeError;
# * a data: path skips dot-segment removal (RFC 2397 payloads are opaque).
_ERRORS = "surrogateescape"


def _oracle_parse(uri: str):
    parsed = uritools.urisplit(uri)
    scheme = parsed.getscheme()
    if scheme == "data":
        path = uritools.uridecode(parsed.path, errors=_ERRORS)
    else:
        path = parsed.getpath(errors=_ERRORS)
    return (
        scheme,
        parsed.getuserinfo(errors=_ERRORS),
        parsed.gethost(errors=_ERRORS) or "",
        parsed.getport(),
        path,
        parsed.query or "",
        parsed.getfragment(errors=_ERRORS) or "",
    )


def _fast_parse(uri: str):
    scheme, authority, path, query, fragment = uritools.urisplit(uri)
    scheme = scheme.lower() if scheme is not None else None
    userinfo, host, port = _split_authority(authority)
    if userinfo is not None:
        userinfo = uritools.uridecode(userinfo, errors=_ERRORS)
    host = _decode_host(host) if host is not None else ""
    if scheme != "data":
        path = _remove_dot_segments(path)
    path = uritools.uridecode(path, errors=_ERRORS)
    fragment = (
        uritools.uridecode(fragment, errors=_ERRORS) if fragment is not None else None
    )
    return scheme, userinfo, host, port, path, (query or ""), (fragment or "")


@given(uri=_uri_string())
@settings(max_examples=1000)
def test_fast_parse_matches_uritools_getters(uri):
    try:
        fast = _fast_parse(uri)
    except Exception as e:
        fast = ("EXC", type(e).__name__)
    try:
        oracle = _oracle_parse(uri)
    except Exception as e:
        oracle = ("EXC", type(e).__name__)
    assert fast == oracle, f"uri={uri!r}"


@given(uri=_uri_string())
@settings(max_examples=500)
def test_uri_parse_uri_matches_oracle_components(uri):
    """Same oracle check, but through the real `Uri._parse_uri` (not just
    the standalone helper functions), so a future refactor that changes
    how they're wired together is still caught."""
    try:
        source, path, query, fragment = Uri._parse_uri(uri)
        fast = (
            source.scheme,
            source.userinfo,
            source.host,
            source.port,
            path,
            str(query),
            fragment,
        )
    except Exception as e:
        fast = ("EXC", type(e).__name__)
    try:
        oracle = _oracle_parse(uri)
    except Exception as e:
        oracle = ("EXC", type(e).__name__)
    assert fast == oracle, f"uri={uri!r}"


_compose_scheme_st = st.one_of(st.none(), st.just(""), _scheme_st)
_compose_userinfo_st = st.one_of(st.none(), st.just(""), _userinfo_st)
_compose_host_st = st.one_of(
    st.none(),
    st.just(""),
    st.text(alphabet="abcXYZ012.-%~!$&'()*+,;=", max_size=15),
    st.builds(lambda: ipaddress.IPv6Address("::1")),
    st.builds(lambda: ipaddress.IPv6Address("2001:db8::1")),
    st.builds(lambda: ipaddress.IPv4Address("192.168.1.1")),
    st.just("[::1]"),
    st.just("::1"),
)
_compose_port_st = st.one_of(st.none(), st.integers(min_value=0, max_value=99999))
_compose_query_st = st.one_of(
    st.none(),
    st.just(""),
    st.text(alphabet="abcXYZ012=&%20+!$&'()*+,;=:@/?~", max_size=20),
)
_compose_frag_st = st.one_of(
    st.none(),
    st.just(""),
    st.text(alphabet="abcXYZ012%20-_!$&'()*+,;=:@/?~", max_size=15),
)


_HEX = "0123456789abcdefABCDEF"


def _oracle_raw_query(query: str) -> str:
    """Design Q1: a stored query is already encoded, so composing keeps every
    valid %XX escape and every delimiter, and encodes only characters that
    cannot appear in a query (uritools.uricompose encodes "%" itself, which
    double-encoded a received query)."""
    out = []
    index = 0
    while index < len(query):
        char = query[index]
        escape = query[index + 1 : index + 3]
        if char == "%" and len(escape) == 2 and all(h in _HEX for h in escape):
            out.append(query[index : index + 3])
            index += 3
            continue
        out.append(uritools.uriencode(char, "!$&'()*+,;=:@/?").decode())
        index += 1
    return "".join(out)


def _with_raw_query(uri: str, query) -> str:
    if query is None:
        return uri
    head, sep, fragment = uri.partition("#")
    return f"{head}?{_oracle_raw_query(query)}{sep}{fragment}"


def _oracle_format_parsed_parts(source, path, query, fragment, sanitize=True):
    parts = {"path": path}
    if fragment:
        parts["fragment"] = fragment
    if source:
        source_ = source._asdict()
        if sanitize:
            source_["userinfo"] = (source_["userinfo"] or "").split(":", maxsplit=1)[0]
        parts.update(source_)
    return _with_raw_query(
        uritools.uricompose(**{k: v for k, v in parts.items() if v}), query or None
    )


@given(
    scheme=_compose_scheme_st,
    userinfo=_compose_userinfo_st,
    host=_compose_host_st,
    port=_compose_port_st,
    path=_path_st,
    query=_compose_query_st,
    fragment=_compose_frag_st,
    sanitize=st.booleans(),
)
@settings(max_examples=1000)
def test_uri_format_parsed_parts_matches_uricompose(
    scheme, userinfo, host, port, path, query, fragment, sanitize
):
    source = Source(scheme, userinfo, host, port)
    try:
        fast = Uri._format_parsed_parts(
            source, path, query, fragment, sanitize=sanitize
        )
    except Exception as e:
        fast = ("EXC", type(e).__name__)
    try:
        oracle = _oracle_format_parsed_parts(source, path, query, fragment, sanitize)
    except Exception as e:
        oracle = ("EXC", type(e).__name__)
    assert (
        fast == oracle
    ), f"source={source!r} path={path!r} query={query!r} fragment={fragment!r} sanitize={sanitize}"


@given(
    # `_compose_uri` is a low-level primitive: it expects an
    # already-lowercased scheme (its callers -- `_format_parsed_parts`,
    # `DavPath._wire_uri` -- do the lowering themselves before calling it,
    # matching how it's actually used in production), so this doesn't
    # generate uppercase schemes -- that's `_format_parsed_parts`'s
    # contract, already covered by the test above.
    scheme=st.one_of(st.none(), st.just("http"), st.just("https")),
    userinfo=_compose_userinfo_st,
    host=_compose_host_st,
    port=_compose_port_st,
    path=_path_st,
    query=_compose_query_st,
    fragment=_compose_frag_st,
)
@settings(max_examples=300)
def test_compose_uri_matches_uricompose_direct(
    scheme, userinfo, host, port, path, query, fragment
):
    """`_compose_uri` used directly (not via the truthy-filtering
    `_format_parsed_parts` wrapper) -- this is DavPath._wire_uri()'s call
    shape: None-checked, not truthy-filtered."""
    try:
        fast = _compose_uri(
            scheme, userinfo, host, port, path, query or None, fragment or None
        )
    except Exception as e:
        fast = ("EXC", type(e).__name__)
    try:
        oracle = _with_raw_query(
            uritools.uricompose(
                scheme=scheme,
                userinfo=userinfo,
                host=host,
                port=port,
                path=path,
                fragment=fragment or None,
            ),
            query or None,
        )
    except Exception as e:
        oracle = ("EXC", type(e).__name__)
    assert (
        fast == oracle
    ), f"scheme={scheme!r} userinfo={userinfo!r} host={host!r} port={port!r}"
