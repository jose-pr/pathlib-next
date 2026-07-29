import os
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


def test_fspath_raises_for_non_host_filesystem_scheme():
    # Uri (pure, no _host_filesystem_path override) must keep raising for a
    # remote scheme whose .path has no meaning as a filesystem path -- e.g.
    # http: is a URL path component, not a path on the host's filesystem.
    uri = Uri("http://user:pass@example.com/a/b")
    with pytest.raises(NotImplementedError):
        os.fspath(uri)
    with pytest.raises(NotImplementedError):
        uri.host_fspath()


def test_str_sanitizes_password_but_as_uri_full_round_trips():
    uri = Uri("http://user:pass@example.com/a/b")
    assert str(uri) == "http://user@example.com/a/b"
    assert uri.as_uri(sanitize=False) == "http://user:pass@example.com/a/b"


def test_source_str_and_repr_redact_password():
    # Source.__str__ used to call uricompose() with the raw userinfo
    # (password included) -- a genuinely valid, authenticated URI string,
    # and exactly the leak: repr() (NamedTuple's default, also unredacted)
    # is what a traceback frame renders, so a Source anywhere on a failing
    # call stack leaked the credential, even though Uri.__str__() already
    # redacted. Both now redact the same way Uri.__str__() does.
    from pathlib_next.uri.source import Source

    source = Source("sftp", "root:secret", "nas", 22)
    assert "secret" not in str(source)
    assert "secret" not in repr(source)
    assert str(source) == "sftp://root@nas:22"
    assert repr(source) == "Source(scheme='sftp', userinfo='root', host='nas', port=22)"


def test_source_str_no_longer_reconstructs_authenticated_uri():
    # This is a deliberate behavior change, not just an addition: str(source)
    # used to be a valid, connectable URI (password included) -- code that
    # relied on f"{source}"/str(source) to rebuild a working authority
    # string would now silently get a non-authenticating one instead.
    # Verified nothing in this codebase does that (every real connection
    # site reads individual Source fields -- .host/.port/.userinfo/
    # parsed_userinfo() -- never whole-object str()). The real round trip,
    # for any caller that genuinely needs it, is uricompose() directly with
    # the unredacted fields (same escape hatch as Uri.as_uri(sanitize=False)).
    import uritools

    from pathlib_next.uri.source import Source

    source = Source("sftp", "root:secret", "nas", 22)
    assert str(source) != "sftp://root:secret@nas:22"
    full = uritools.uricompose(
        scheme=source.scheme,
        userinfo=source.userinfo,
        host=source.host,
        port=source.port,
    )
    assert full == "sftp://root:secret@nas:22"


def test_source_userinfo_field_still_carries_password():
    # Redaction is display-only (__str__/__repr__) -- the actual data
    # access API (.userinfo, parsed_userinfo(), keys()/__getitem__) must
    # still return the real password; only rendering is sanitized.
    from pathlib_next.uri.source import Source

    source = Source("sftp", "root:secret", "nas", 22)
    assert source.userinfo == "root:secret"
    assert source.parsed_userinfo() == ("root", "secret")
    assert source["userinfo"] == "root:secret"


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
