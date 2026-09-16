import pytest

from pathlib_next.uri import Source, Uri, UriPath
from pathlib_next.uri.schemes.file import FileUri


def test_scheme_dispatch_file():
    p = UriPath("file:/a/b")
    assert isinstance(p, FileUri)


def test_scheme_dispatch_unknown_scheme_falls_back_to_uripath():
    p = UriPath("customscheme://host/a/b")
    assert type(p) is UriPath


def test_backend_propagation_on_truediv(tmp_path):
    root = FileUri(tmp_path.as_uri())
    child = root / "sub" / "file.txt"
    assert child.backend is root.backend


def test_with_source_backend_preserved(tmp_path):
    root = FileUri(tmp_path.as_uri())
    other_source = Source("file", None, None, None)
    retargeted = root.with_source(other_source)
    assert retargeted.backend is root.backend


# --- B27 (documented divergence): Uri("a").parent -> "" (empty path,
# round-trips) instead of pathlib's "." ---


def test_parent_of_single_segment_is_empty_not_dot():
    p = Uri("a")
    assert p.parent.path == ""
    assert p.parent.as_posix() == ""


def test_empty_uri_round_trips_through_parent():
    empty = Uri("")
    assert empty.parent.path == empty.path == ""


# --- B28 (documented divergence): with_name/with_suffix/with_stem keep
# query/fragment ---


def test_with_suffix_keeps_query_fragment():
    p = UriPath("http://h/a.txt?x=1#frag")
    renamed = p.with_suffix(".py")
    assert renamed.name == "a.py"
    assert renamed.query == "x=1"
    assert renamed.fragment == "frag"


def test_with_name_keeps_query_fragment():
    p = UriPath("http://h/a.txt?x=1#frag")
    renamed = p.with_name("b.txt")
    assert renamed.name == "b.txt"
    assert renamed.query == "x=1"
    assert renamed.fragment == "frag"


# --- B8: Uri hashable, __eq__ contract ---


def test_uri_hashable():
    u1 = Uri("http://h/a")
    u2 = Uri("http://h/a")
    assert hash(u1) == hash(u2)
    assert {u1, u2} == {u1}


def test_uri_eq_with_str():
    u = Uri("http://h/a")
    assert u == u.as_uri()


def test_uri_eq_notimplemented_for_unrelated_type():
    u = Uri("http://h/a")
    assert (u == 42) is False
    assert (u != 42) is True


def test_uri_usable_as_dict_key():
    u1 = Uri("http://h/a")
    u2 = Uri("http://h/a")
    d = {u1: "value"}
    assert d[u2] == "value"


# --- B30 (documented divergence): Path.__iter__ is iterdir ---


def test_iter_is_iterdir(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    root = FileUri(tmp_path.as_uri())
    names = {p.name for p in root}
    assert names == {"a.txt"}


def test_relative_to_errors():
    u1 = Uri("http://host1/a/b")
    u2 = Uri("http://host2/c")
    # Different anchors/hosts -> ValueError
    with pytest.raises(ValueError) as exc:
        u1.relative_to(u2, walk_up=True)
    assert "have different anchors" in str(exc.value)

    # Different subpaths -> ValueError
    u3 = Uri("http://host1/a/b")
    u4 = Uri("http://host1/c")
    with pytest.raises(ValueError) as exc:
        u3.relative_to(u4, walk_up=False)
    assert "is not in the subpath of" in str(exc.value)

    # Walk up containing '..' segment -> ValueError
    u5 = Uri("a/b")
    u6 = Uri("../d")
    with pytest.raises(ValueError) as exc:
        u5.relative_to(u6, walk_up=True)
    assert "cannot be walked" in str(exc.value)


def test_uri_with_fragment_and_segments():
    u = Uri("http://host/a?query=1#frag")
    # test with_fragment
    u_f = u.with_fragment("newfrag")
    assert u_f.fragment == "newfrag"
    assert u_f.query == "query=1"

    # test with_segments empty
    u_seg = u.with_segments()
    assert u_seg.path == ""


def test_uripath_unimplemented_unlink():
    # UriPath itself is abstract and doesn't implement unlink
    p = UriPath("custom://host/a")
    with pytest.raises(NotImplementedError):
        p.unlink()


# --- destination/target normalization (0.9.3) ----------------------------
#
# The single place every scheme's `rename()` and `symlink_to()` now converts
# a `str` destination. Tested here, once, rather than per-scheme: a `str`
# destination is an already-decoded PATH, so feeding it back through the URI
# parser truncated it at "?"/"#", percent-decoded it, and read a leading
# "C:" as a scheme -- silently, on the wire.

_DECODED = ["rn?b.txt", "rn#b.txt", "rn b.txt", "rn%20b.txt", "rn%b.txt", "rn:b.txt"]


@pytest.mark.parametrize("name", _DECODED)
def test_rename_target_str_is_a_sibling_literal_path(name):
    p = UriPath("customscheme://host/mnt/a.txt")
    assert p._rename_target(name).path == f"/mnt/{name}"


@pytest.mark.parametrize("name", _DECODED)
def test_rename_target_absolute_str_is_a_literal_path(name):
    p = UriPath("customscheme://host/mnt/a.txt")
    assert p._rename_target(f"/other/{name}").path == f"/other/{name}"


@pytest.mark.parametrize("name", _DECODED)
def test_rename_target_uri_is_taken_as_given(name):
    p = UriPath("customscheme://host/mnt/a.txt")
    target = Uri("customscheme://host/mnt/x")._from_decoded_path(f"/mnt/{name}")
    # An already-built path object must pass straight through: no second
    # encode/decode round on top of whatever built it (consumers that
    # percent-encode a decoded path and construct from the resulting URI
    # would otherwise see a literal "%20" come back as a space).
    assert p._rename_target(target).path == f"/mnt/{name}"


@pytest.mark.parametrize("name", _DECODED)
def test_symlink_target_str_is_literal_and_never_anchored(name):
    p = UriPath("customscheme://host/mnt/link")
    # Relative stays relative -- unlike rename(), a symlink target is
    # stored verbatim (pathlib.Path.symlink_to() parity).
    assert p._symlink_target(name).path == name
    assert p._symlink_target(f"/mnt/{name}").path == f"/mnt/{name}"


def test_symlink_target_keeps_dot_dot_relative():
    p = UriPath("customscheme://host/mnt/sub/link")
    assert p._symlink_target("../real.txt").path == "../real.txt"


def test_from_decoded_path_keeps_backend_and_drops_query_fragment():
    p = UriPath("customscheme://host/mnt/a.txt?q=1#f")
    target = p._from_decoded_path("/mnt/b?c#d.txt")
    assert target.path == "/mnt/b?c#d.txt"
    # self's own query/fragment must not leak onto a destination.
    assert not target.query
    assert not target.fragment
    assert target.backend is p.backend


# --- a str join is a decoded path, not URI syntax -------------------------

#: `(joined name, resulting .path)`. Every one of these was silently
#: truncated, decoded or re-anchored when `/` parsed the name as URI syntax.
_DECODED_JOINS = [
    ("report.txt", "/mnt/report.txt"),
    ("cache?v=2", "/mnt/cache?v=2"),
    ("note#2.txt", "/mnt/note#2.txt"),
    ("a%20b.txt", "/mnt/a%20b.txt"),
    ("C:/Temp", "/mnt/C:/Temp"),
    ("sub/deep.txt", "/mnt/sub/deep.txt"),
    ("/abs.txt", "/abs.txt"),
    ("a/../b", "/mnt/b"),
    ("../up.txt", "/up.txt"),
]


@pytest.mark.parametrize("name,expected", _DECODED_JOINS)
def test_join_reads_a_str_as_a_decoded_path(name, expected):
    base = UriPath("sftp://host/mnt")
    assert (base / name).path == expected
    assert base.joinpath(name).path == expected


def test_join_agrees_with_a_listing_for_the_same_name():
    """The inconsistency this replaces: `iterdir()` built `cache?v=2`
    correctly while `/` truncated it at the `?`."""
    base = UriPath("sftp://host/mnt")
    assert (base / "cache?v=2").path == base._make_child_relpath("cache?v=2").path


def test_join_percent_encodes_a_literal_name_when_rendered():
    base = UriPath("sftp://host/mnt")
    assert str(base / "cache?v=2") == "sftp://host/mnt/cache%3Fv=2"
    assert (base / "cache?v=2").name == "cache?v=2"


def test_join_with_a_uri_argument_is_still_scheme_aware():
    """The escape hatch: pass a `Uri` when URI semantics are wanted."""
    base = UriPath("sftp://host/mnt")
    crossed = base / UriPath("s3://bucket/key")
    assert str(crossed) == "s3://bucket/key"
    assert crossed.source.scheme == "s3"


def test_join_keeps_the_query_of_a_uri_argument():
    base = UriPath("http://h/a")
    assert (base / UriPath("b?q=1")).query == "q=1"
    assert (base / "b?q=1").query in (None, "")


# --- copy()/move() read a str destination by its shape --------------------


@pytest.mark.parametrize(
    "target,expected",
    [
        ("b.txt", "sftp://user@host/mnt/b.txt"),
        ("sub/b.txt", "sftp://user@host/mnt/sub/b.txt"),
        ("/other/b.txt", "sftp://user@host/other/b.txt"),
        ("C:/Temp/x", "sftp://user@host/mnt/C:/Temp/x"),
    ],
)
def test_str_destination_without_a_scheme_stays_on_this_endpoint(target, expected):
    """A plain path names a file on the same URI, as `rename()` does --
    before, it built a sourceless path that could not do I/O at all."""
    src = UriPath("sftp://user@host/mnt/a.txt")
    coerced = src._coerce_target(target)
    assert str(coerced) == expected
    assert coerced.source == src.source


@pytest.mark.parametrize(
    "target", ["s3://bucket/key", "file:///tmp/x", "data:,abc", "http://h/x"]
)
def test_str_destination_with_a_scheme_is_still_a_uri(target):
    """The cross-scheme form keeps working; that is why the rule is by
    shape rather than path-only."""
    src = UriPath("sftp://user@host/mnt/a.txt")
    assert src._coerce_target(target).source.scheme == target.split(":", 1)[0]


def test_str_destination_reuses_the_backend_of_the_same_endpoint():
    """No second connection for a destination next to the source."""
    requests = pytest.importorskip("requests")
    base = UriPath("http://h/api/a.txt").with_session(requests.Session())
    assert base._coerce_target("b.txt").backend is base.backend


# --- join review findings (2026-09-16) ------------------------------------


@pytest.mark.parametrize(
    "base,joined",
    [("data:,a/./b", "data:,a/./b/x"), ("data:,a/../b", "data:,a/../b/x")],
)
def test_join_never_normalizes_a_data_payload(base, joined):
    """An RFC 2397 payload is an opaque octet string -- `_parse_uri` says so
    and skips dot-segment removal for `data:`. Normalizing the joined result
    ate the `,` that separates the payload, producing `data:b/x`, which is
    not a data URI at all."""
    assert str(UriPath(base) / "x") == joined


def test_join_uses_the_same_child_builder_as_a_listing():
    """`/` walks segments through `_make_child_relpath()`, so a scheme that
    gives a name special meaning sees it. `gitlab:` reserves "-": built by
    hand the child used to address the repository root instead of the
    directory the listing yields."""
    gitlab = pytest.importorskip("pathlib_next.uri.schemes.gitlab")
    repo = gitlab.GitLabPath("gitlab://gitlab.com/owner/repo")
    assert (repo / "-").path == repo._make_child_relpath("-").path


def test_join_reads_bytes_as_a_decoded_path_too():
    """`bytes` was the one plain-name form still going through the parser."""
    base = UriPath("sftp://host/mnt")
    assert (base / b"cache?v=2").path == (base / "cache?v=2").path == "/mnt/cache?v=2"


@pytest.mark.parametrize(
    "value,is_uri",
    [
        ("s3://bucket/key", True),
        ("data:,abc", True),
        ("file:/x", True),
        # A relative name whose first segment merely contains a colon.
        ("notes:draft", False),
        ("Fedora-42:latest.tar", False),
        ("12:30.txt", False),
        ("C:/Temp", False),  # a drive, not a one-letter scheme
    ],
)
def test_a_str_destination_is_only_a_uri_for_a_registered_scheme(value, is_uri):
    from pathlib_next.uri import _looks_like_uri

    assert _looks_like_uri(value) is is_uri


def test_rename_and_copy_resolve_a_relative_str_the_same_way():
    """They disagreed on `..`: rename sent the literal `sub/../b.txt`, which
    an object store reads as a different key than the `/mnt/b.txt` move()
    writes."""
    src = UriPath("sftp://user@host/mnt/sub/a.txt")
    for target in ("b.txt", "../b.txt", "./b.txt"):
        assert src._rename_target(target).path == src._coerce_target(target).path


def test_a_same_endpoint_uri_destination_keeps_the_configured_backend():
    """`copy("http://same-host/...")` opened a second, unauthenticated
    session against the host the caller had just authenticated to."""
    requests = pytest.importorskip("requests")
    base = UriPath("http://trusted.invalid/api/a.txt").with_session(requests.Session())
    assert (
        base._coerce_target("http://trusted.invalid/api/b.txt").backend is base.backend
    )
    assert base._coerce_target("http://other.invalid/b.txt").backend is not base.backend
