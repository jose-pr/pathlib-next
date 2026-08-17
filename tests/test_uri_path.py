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
