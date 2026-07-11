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
