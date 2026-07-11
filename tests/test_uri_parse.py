import pathlib

import pytest

import pathlib_next
from pathlib_next.uri import Uri

test_uris = ["http://user:pass@google.com:80"]


# @pytest.mark.parametrize("_uri", test_uris)
def parse_uri(_uri: str):
    uri = pathlib_next.Uri(_uri)
    assert uri.as_uri() == _uri
    return uri


def test_source():
    uri = parse_uri(test_uris[0])
    assert uri.source.scheme == "http"
    assert uri.source.host == "google.com"
    assert uri.source.port == 80
    assert uri.source.parsed_userinfo() == ("user", "pass")


def test_no_scheme():
    uri = parse_uri("//user:pass@google.com:80/")
    assert uri.source.scheme == None
    assert uri.source.host == "google.com"
    assert uri.source.port == 80
    assert uri.source.parsed_userinfo() == ("user", "pass")


def test_no_scheme_with_host_no_pass():
    uri = parse_uri("//user@google.com:80/")
    assert uri.source.scheme == None
    assert uri.source.host == "google.com"
    assert uri.source.port == 80
    assert uri.source.parsed_userinfo() == ("user", "")


def test_no_scheme_no_host():
    uri = parse_uri("//user@:80/")
    assert uri.source.scheme == None
    assert uri.source.host == ""
    assert uri.source.port == 80
    assert uri.source.parsed_userinfo() == ("user", "")


def test_no_scheme_no_netloc():
    uri = parse_uri("//user@")
    assert uri.source.scheme == None
    assert uri.source.host == ""
    assert uri.source.port == None
    assert uri.source.parsed_userinfo() == ("user", "")


def test_path():
    uri = parse_uri("http://google.com/root/subroot/filename.ext")
    assert uri.source.scheme == "http"
    assert uri.source.host == "google.com"
    assert uri.source.port == None
    assert uri.source.parsed_userinfo() == ("", "")
    assert uri.path == "/root/subroot/filename.ext"


def test_encoded_path():
    uri = pathlib_next.Uri(
        "http://goog%2Fe.com/root/subroot/%3Fquery/%23fragment/%2Fencoded%2Ffilename.ext"
    )
    assert uri.source.scheme == "http"
    assert uri.source.host == "goog/e.com"
    assert uri.source.port == None
    assert uri.source.parsed_userinfo() == ("", "")
    assert uri.path == "/root/subroot/?query/#fragment//encoded/filename.ext"


def test_child():
    sftp_root = Uri("sftp://root@sftpexample/")
    authkeys = sftp_root / "root/.ssh/authorized_keys"
    uri = authkeys.as_uri()
    assert uri == "sftp://root@sftpexample/root/.ssh/authorized_keys"


def test_truediv_pathlib():
    sftp_root = Uri("sftp://root@sftpexample/")
    authkeys = sftp_root / pathlib.PurePosixPath("root/.ssh/authorized_keys")
    uri = authkeys.as_uri()
    assert uri == "sftp://root@sftpexample/root/.ssh/authorized_keys"

    authkeys = sftp_root / pathlib.Path("root/.ssh/authorized_keys")
    uri = authkeys.as_uri()
    assert uri == "file:/root/.ssh/authorized_keys"


def test_join_without_root():
    authkeys = Uri("sftp://root@sftpexample") / "root/.ssh/authorized_keys"
    uri = authkeys.as_uri()
    assert uri == "sftp://root@sftpexample/root/.ssh/authorized_keys"


# --- B15 regressions: Uri.__init__ from various source types ---


def test_from_pure_posix_path():
    # PurePosixPath isn't a pathlib.Path (no as_uri()), so this exercises
    # the plain-Pathname/PurePath branch of Uri.__init__.
    uri = Uri(pathlib.PurePosixPath("a/b/c"))
    assert uri.path.endswith("a/b/c")


def test_from_fspath_only_object():
    # B15: constructing from an object that only implements __fspath__ (no
    # as_posix()) used to crash with AttributeError.
    class FspathOnly:
        def __fspath__(self):
            return "a/b/c.txt"

    uri = Uri(FspathOnly())
    assert uri.path.endswith("a/b/c.txt")


def test_from_relative_local_path_no_crash():
    # pathlib.Path.as_uri() raises ValueError for relative paths; Uri()
    # must fall back cleanly instead of propagating a bare except.
    uri = Uri(pathlib.Path("relative/path.txt"))
    assert "relative/path.txt" in uri.path


# --- B26 regression: query/fragment "last segment that sets one wins" ---


def test_join_query_fragment_last_setting_segment_wins():
    # A later segment with NO query/fragment must not blank out an earlier
    # segment's -- only a later segment that actually sets one should win.
    base = Uri("http://h/a?x=1#frag")
    joined = Uri(base, "b")
    assert joined.query == "x=1"
    assert joined.fragment == "frag"


def test_join_query_fragment_later_segment_overrides():
    base = Uri("http://h/a?x=1")
    joined = Uri(base, "b?y=2#frag2")
    assert joined.query == "y=2"
    assert joined.fragment == "frag2"
