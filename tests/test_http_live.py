"""End-to-end coverage of
`_DirectoryListingParser`'s two primary (non-fallback) code paths against
real servers, real write/delete persistence, the exception-translation
matrix beyond 404/405/501, and `stat()`'s redirect/HEAD-fallback branches.
`tests/test_http.py`'s `http_server` fixture (stdlib
`SimpleHTTPRequestHandler`) only ever emits `<ul><li>` listings, so none of
this was previously exercised by anything but the fallback branch.
"""
import pytest

pytest.importorskip("requests")

import requests

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.http import HttpPath


def test_apache_pre_listing_end_to_end(http_server_apache_pre):
    root = UriPath(f"{http_server_apache_pre}/")
    names = {p.name for p in root.iterdir()}
    assert names == {"a.txt", "b.py", ".hidden.txt", "sub", "empty_dir"}

    entries = {e.name: e for e in HttpPath(f"{http_server_apache_pre}/")._listdir()}
    assert entries["a.txt"].size == 1
    assert entries["a.txt"].modified is not None
    # directories are rendered with size "-" in the listing -> None in the parser
    assert entries["sub/"].size is None
    assert entries["sub/"].modified is not None


def test_nginx_table_listing_end_to_end(http_server_nginx_table):
    root = UriPath(f"{http_server_nginx_table}/")
    names = {p.name for p in root.iterdir()}
    assert names == {"a.txt", "b.py", ".hidden.txt", "sub", "empty_dir"}

    entries = {e.name: e for e in HttpPath(f"{http_server_nginx_table}/")._listdir()}
    assert entries["a.txt"].size == 1
    assert entries["a.txt"].modified is not None


def test_recursive_glob_apache_pre(http_server_apache_pre):
    root = UriPath(f"{http_server_apache_pre}/")
    names = {p.name for p in root.glob("**/*.py")}
    assert names == {"b.py", "c.py", "d.py"}


def test_recursive_glob_nginx_table(http_server_nginx_table):
    root = UriPath(f"{http_server_nginx_table}/")
    names = {p.name for p in root.glob("**/*.py")}
    assert names == {"b.py", "c.py", "d.py"}


def test_write_then_read_back_round_trip(http_writable_server):
    p = UriPath(f"{http_writable_server}/new_file.txt")
    p.write_text("round trip content")
    # A fresh path object (and fresh GET) confirms server-side persistence,
    # not just that the client believes the write succeeded.
    p2 = UriPath(f"{http_writable_server}/new_file.txt")
    assert p2.read_text() == "round trip content"
    assert p2.stat().st_size == len(b"round trip content")


def test_rmdir_on_file_raises_notadirectoryerror(http_writable_server):
    p = UriPath(f"{http_writable_server}/a.txt")
    with pytest.raises(NotADirectoryError):
        p.rmdir()
    # must not have been deleted
    assert p.exists()


def test_write_then_delete_round_trip(http_writable_server):
    p = UriPath(f"{http_writable_server}/to_delete.txt")
    p.write_text("temp")
    assert p.exists()
    p.unlink()
    assert not UriPath(f"{http_writable_server}/to_delete.txt").exists()


@pytest.mark.parametrize(
    "status,exc_type",
    [
        (401, PermissionError),
        (403, PermissionError),
        (409, FileExistsError),
    ],
)
def test_exception_translation_matrix(http_status_server, status, exc_type):
    with pytest.raises(exc_type):
        UriPath(f"{http_status_server}/{status}").read_text()


def test_exception_translation_timeout(http_status_server):
    p = UriPath(f"{http_status_server}/timeout")
    p = p.with_session(requests.Session(), timeout=0.5)
    with pytest.raises(TimeoutError):
        p.read_text()


def test_exception_translation_connection_error(unused_tcp_port):
    p = UriPath(f"http://127.0.0.1:{unused_tcp_port}/x")
    with pytest.raises(ConnectionError):
        p.read_text()


def test_stat_head_405_falls_back_to_get(http_server_head_rejecting):
    p = UriPath(f"{http_server_head_rejecting}/a.txt")
    st = p.stat()
    assert st.st_size == 1
    assert not st.is_dir()


def test_stat_redirect_then_head_405_falls_back_to_get(http_server_head_rejecting):
    # "/sub" (no trailing slash): the pre-redirect loop's own HEAD-405
    # fallback kicks in first (GET, allow_redirects=False -> 301), so
    # resp.is_redirect is True and stat() re-fetches HEAD on the redirect
    # target -- which this server also 405s. Regression: that second
    # HEAD had no fallback, so this surfaced PermissionError for a
    # directory that actually exists.
    p = UriPath(f"{http_server_head_rejecting}/sub")
    st = p.stat()
    assert st.is_dir()


def test_stat_redirect_branch_reports_dir(http_server_apache_pre):
    # "/sub" (no trailing slash) -- server 301s to "/sub/"; stat()'s
    # allow_redirects=False HEAD sees the redirect, computes is_dir from
    # it, then re-fetches HEAD on the redirect target.
    p = UriPath(f"{http_server_apache_pre}/sub")
    st = p.stat()
    assert st.is_dir()
    assert st.st_size == 0
