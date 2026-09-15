"""Data-loss regressions for HttpPath/DavPath against real loopback servers.

`dav_server` is wsgidav; the same server addressed through an `http:` URL
exercises wsgidav's own HTML directory listing, whose parent row is
`<a href="..">..</a>`.
"""

import errno
import functools
import http.server
import threading
import time

import pytest

pytest.importorskip("requests")

from pathlib_next import LocalPath
from pathlib_next.uri.schemes.dav import DavPath
from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.http import HttpPath


def _http(dav_url):
    return "http" + dav_url[len("dav") :]


# --- httpdav-dotdot-listing-entry-deletes-parent ---


def test_http_listing_of_wsgidav_never_yields_dot_entries(dav_server, fixture_tree):
    sub = HttpPath(f"{_http(dav_server)}/sub/")
    children = list(sub.iterdir())
    assert sorted(c.name for c in children) == ["c.py", "nested"]
    assert all(c.path.rsplit("/", 1)[-1] not in (".", "..") for c in children)


def test_http_unlink_every_file_in_listing_keeps_parent(dav_server, fixture_tree):
    # The reported loop: with ".." listed as a file, its unlink() sent
    # DELETE /sub/.. -> DELETE / and wsgidav removed the whole share.
    sub = HttpPath(f"{_http(dav_server)}/sub/")
    for child in sub.iterdir():
        if child.is_file():
            child.unlink()
    assert not (fixture_tree / "sub" / "c.py").exists()
    assert (fixture_tree / "sub" / "nested" / "d.py").read_text() == "d"
    assert (fixture_tree / "a.txt").read_text() == "a"
    assert (fixture_tree / "empty_dir").is_dir()


def test_http_walk_of_wsgidav_terminates(dav_server):
    root = HttpPath(f"{_http(dav_server)}/sub/")
    tops = [top.path for top, _dirs, _files in root.walk()]
    assert len(tops) == 2
    assert all("/./" not in t and "/../" not in t for t in tops)


class _DotListingHandler(http.server.SimpleHTTPRequestHandler):
    """A `<pre>` index that lists "./", "../" and "%2E%2E/" as entries."""

    def list_directory(self, path):
        import io

        html = (
            "<html><head><title>Index of /d/</title></head><body><pre>"
            '<a href="./">./</a>\n<a href="../">../</a>\n'
            '<a href="%2E%2E/">%2E%2E/</a>\n<a href="x.txt">x.txt</a>\n'
            "</pre></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        return io.BytesIO(html)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def dot_listing_server(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x.txt").write_text("x")
    handler = functools.partial(_DotListingHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_pre_listing_dot_entries_are_dropped(dot_listing_server):
    d = HttpPath(f"{dot_listing_server}/d/")
    assert [c.name for c in d.iterdir()] == ["x.txt"]
    # "./" used to make walk() descend /d/./././... without end.
    tops = [top.path for top, _dirs, _files in d.walk()]
    assert tops == ["/d/"]


def test_dav_listing_of_wsgidav_has_no_dot_entries(dav_server):
    names = sorted(c.name for c in DavPath(f"{dav_server}/sub/").iterdir())
    assert names == ["c.py", "nested"]


# --- httpdav-unlink-deletes-collection-tree ---


def test_dav_unlink_on_collection_keeps_tree(dav_server, fixture_tree):
    p = DavPath(f"{dav_server}/sub")
    with pytest.raises(IsADirectoryError) as excinfo:
        p.unlink()
    assert excinfo.value.errno == errno.EISDIR
    with pytest.raises(IsADirectoryError):
        p.unlink(missing_ok=True)
    assert (fixture_tree / "sub" / "c.py").read_text() == "c"
    assert (fixture_tree / "sub" / "nested" / "d.py").read_text() == "d"


def test_dav_symlink_to_force_keeps_collection(dav_server, fixture_tree):
    p = DavPath(f"{dav_server}/sub")
    with pytest.raises(Exception):
        p.symlink_to("x", force=True)
    assert (fixture_tree / "sub" / "nested" / "d.py").read_text() == "d"


def test_dav_unlink_file_and_missing_semantics(dav_server, fixture_tree):
    DavPath(f"{dav_server}/a.txt").unlink()
    assert not (fixture_tree / "a.txt").exists()
    assert (fixture_tree / "b.py").exists()
    DavPath(f"{dav_server}/a.txt").unlink(missing_ok=True)
    with pytest.raises(FileNotFoundError):
        DavPath(f"{dav_server}/a.txt").unlink()


def test_dav_rmdir_and_recursive_rm_still_delete(dav_server, fixture_tree):
    DavPath(f"{dav_server}/empty_dir").rmdir()
    assert not (fixture_tree / "empty_dir").exists()
    DavPath(f"{dav_server}/sub").rm(recursive=True)
    assert not (fixture_tree / "sub").exists()
    assert (fixture_tree / "a.txt").exists()


def test_http_unlink_on_listed_directory_keeps_tree(dav_server, fixture_tree):
    root = HttpPath(f"{_http(dav_server)}/")
    sub = next(c for c in root.iterdir() if c.name == "sub")
    with pytest.raises(IsADirectoryError) as excinfo:
        sub.unlink()
    assert excinfo.value.errno == errno.EISDIR
    assert (fixture_tree / "sub" / "c.py").read_text() == "c"
    assert (fixture_tree / "sub" / "nested" / "d.py").read_text() == "d"


def test_http_unlink_refuses_redirecting_directory(http_server, fixture_tree):
    # The stdlib server 301s /sub -> /sub/, so stat() sees a directory;
    # unlink() must refuse before sending any DELETE.
    with pytest.raises(IsADirectoryError):
        HttpPath(f"{http_server}/sub").unlink()
    assert (fixture_tree / "sub" / "c.py").exists()


# --- httpdav-dav-read-ignores-http-status ---


def test_dav_read_missing_file_raises(dav_server, fixture_tree):
    p = DavPath(f"{dav_server}/missing.txt")
    with pytest.raises(FileNotFoundError):
        p.read_bytes()
    with pytest.raises(FileNotFoundError):
        p.open("rb")
    # Reads of existing files are unaffected, and the connection pool was
    # not exhausted by the failed streams above.
    for _ in range(3):
        with pytest.raises(FileNotFoundError):
            p.read_text()
    assert DavPath(f"{dav_server}/a.txt").read_text() == "a"


def test_dav_copy_missing_source_writes_no_error_page(dav_server, tmp_path):
    out = tmp_path / "out.txt"
    with pytest.raises(FileNotFoundError):
        DavPath(f"{dav_server}/missing.txt").copy(
            LocalPath(out), preserve_metadata=False
        )
    assert not out.exists() or b"<!DOCTYPE" not in out.read_bytes()


# --- wave 5: a scriptable loopback server --------------------------------


_LOCKED_MULTISTATUS = b"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/locked/held%20file.txt</D:href>
    <D:status>HTTP/1.1 423 Locked</D:status>
  </D:response>
</D:multistatus>"""

_PLAIN_BODY = b"line one: the uncompressed body of a text file\n" * 20


class _WireHandler(http.server.BaseHTTPRequestHandler):
    """Routes for the wire-level regressions. `store` holds file bodies
    (PUT writes into it); `seen` records `(method, path, headers)`."""

    store = None
    seen = None

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _get(self):
        import gzip

        path = self.path
        accept = self.headers.get("Accept-Encoding", "")
        if path == "/redirect.txt":
            return self._send(302, headers={"Location": "/real.txt"})
        if path == "/dir":
            return self._send(301, headers={"Location": "/dir/"})
        if path == "/dir/":
            return self._send(200, b"<html><body></body></html>")
        if path == "/lm.txt":
            return self._send(
                200, b"x", {"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"}
            )
        if path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"0123456789")
            self.wfile.flush()
            self.close_connection = True
            return
        if path == "/stall":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"01234")
            self.wfile.flush()
            time.sleep(3)
            return
        body = self.store.get(path)
        if body is None:
            return self._send(404)
        if path.startswith("/always-gz") or "gzip" in accept:
            return self._send(200, gzip.compress(body), {"Content-Encoding": "gzip"})
        return self._send(200, body)

    def do_GET(self):
        self.seen.append((self.command, self.path, dict(self.headers)))
        self._get()

    do_HEAD = do_GET

    def do_PUT(self):
        self.seen.append((self.command, self.path, dict(self.headers)))
        data = self._body()
        if self.path == "/fail.txt":
            return self._send(503)
        self.store[self.path] = data
        self._send(201)

    def do_DELETE(self):
        self.seen.append((self.command, self.path, dict(self.headers)))
        if self.path.startswith("/locked"):
            return self._send(
                207, _LOCKED_MULTISTATUS, {"Content-Type": "application/xml"}
            )
        self._send(204)

    do_MOVE = do_DELETE

    def do_PROPFIND(self):
        self.seen.append((self.command, self.path, dict(self.headers)))
        self._body()
        self._send(200, b"<html><body>hi<br></body></html>")

    def log_message(self, format, *args):
        pass


@pytest.fixture
def wire_server():
    store = {
        "/gz.txt": _PLAIN_BODY,
        "/always-gz.txt": _PLAIN_BODY,
        "/log.txt": _PLAIN_BODY,
        "/real.txt": _PLAIN_BODY,
    }
    seen = []
    handler = type("_Handler", (_WireHandler,), {"store": store, "seen": seen})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", store, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --- httpdav-gzip-content-encoding-not-decoded ---


@pytest.mark.parametrize("scheme", ["http", "dav"])
@pytest.mark.parametrize("name", ["gz.txt", "always-gz.txt"])
def test_read_of_compressing_server_returns_uncompressed_body(
    wire_server, scheme, name
):
    host, _store, _seen = wire_server
    p = UriPath(f"{scheme}://{host}/{name}")
    assert p.read_bytes() == _PLAIN_BODY
    # Small reads through the buffered and the unbuffered stream.
    with p.open("rb") as f:
        assert b"".join(iter(lambda: f.read(7), b"")) == _PLAIN_BODY
    with p.open("rb", buffering=0) as f:
        assert b"".join(iter(lambda: f.read(5), b"")) == _PLAIN_BODY


def test_stat_size_of_compressing_server_is_uncompressed(wire_server):
    host, _store, _seen = wire_server
    assert HttpPath(f"http://{host}/gz.txt").stat().st_size == len(_PLAIN_BODY)


def test_rewrite_append_keeps_plaintext_on_compressing_server(wire_server):
    host, store, _seen = wire_server
    with HttpPath(f"http://{host}/log.txt").open("ab") as f:
        f.write(b"line three\n")
    assert store["/log.txt"] == _PLAIN_BODY + b"line three\n"


# --- httpdav-read-errors-escape-oserror ---


def test_truncated_body_raises_oserror(wire_server):
    host, _store, _seen = wire_server
    with pytest.raises(OSError):
        HttpPath(f"http://{host}/truncated").read_bytes()


def test_body_stalled_after_headers_raises_timeouterror(wire_server):
    import requests

    host, _store, _seen = wire_server
    p = HttpPath(f"http://{host}/stall").with_session(requests.Session(), timeout=1)
    with pytest.raises(TimeoutError):
        p.read_bytes()


# --- httpdav-stat-redirect-means-directory ---


def test_stat_redirect_to_file_is_a_file(wire_server, tmp_path):
    host, _store, _seen = wire_server
    p = UriPath(f"http://{host}/redirect.txt")
    st = p.stat()
    assert not st.is_dir()
    assert st.st_size == len(_PLAIN_BODY)
    assert p.is_file()
    out = tmp_path / "out.txt"
    UriPath(f"http://{host}/redirect.txt").copy(LocalPath(out), recursive=True)
    assert out.is_file()
    assert out.read_bytes() == _PLAIN_BODY


def test_stat_redirect_to_slash_is_a_directory(wire_server):
    host, _store, _seen = wire_server
    assert UriPath(f"http://{host}/dir").stat().is_dir()


def test_stat_trailing_slash_directory_answered_both_ways(dav_server):
    # wsgidav answers HEAD /sub and HEAD /sub/ with 200.
    assert HttpPath(f"{_http(dav_server)}/sub/").stat().is_dir()
    assert HttpPath(f"{_http(dav_server)}/a.txt").stat().is_file()


# --- httpdav-last-modified-parsed-as-local-time ---


def test_http_last_modified_is_utc(wire_server):
    import calendar

    host, _store, _seen = wire_server
    st = HttpPath(f"http://{host}/lm.txt").stat()
    assert st.st_mtime == calendar.timegm((2024, 1, 1, 0, 0, 0))


# --- httpdav-requests-args-collide-with-internal-kwargs ---


def test_with_session_args_do_not_collide(wire_server, dav_server):
    import requests

    host, _store, seen = wire_server
    p = HttpPath(f"http://{host}/redirect.txt").with_session(
        requests.Session(),
        allow_redirects=True,
        stream=False,
        headers={"X-Token": "t"},
    )
    assert p.stat().is_file()
    assert p.read_bytes() == _PLAIN_BODY
    assert seen and all(headers.get("X-Token") == "t" for _m, _p, headers in seen)

    d = DavPath(f"{dav_server}/sub").with_session(
        requests.Session(), headers={"X-Token": "t"}
    )
    assert d.is_dir()
    assert sorted(c.name for c in d.iterdir()) == ["c.py", "nested"]


# --- httpdav-dav-write-stream-retries-put-on-gc ---


def test_dav_failed_put_is_not_resent_at_gc(wire_server):
    import gc

    host, _store, seen = wire_server
    f = DavPath(f"dav://{host}/fail.txt").open("wb")
    f.write(b"stale")
    with pytest.raises(OSError):
        f.close()
    assert f.closed
    del f
    gc.collect()
    assert [m for m, path, _h in seen if path == "/fail.txt"] == ["PUT"]


# --- httpdav-dav-errors-untranslated ---


def test_dav_write_into_missing_parent_raises_filenotfound(dav_server):
    with pytest.raises(FileNotFoundError):
        DavPath(f"{dav_server}/nodir/f.txt").write_bytes(b"x")


def test_dav_non_xml_propfind_reads_as_missing(wire_server):
    host, _store, _seen = wire_server
    p = DavPath(f"dav://{host}/html")
    assert p.exists() is False
    assert p.is_dir() is False
    with pytest.raises(OSError):
        p.stat()


# --- httpdav-dav-207-multistatus-treated-as-success ---


def test_dav_207_delete_with_locked_member_raises(wire_server):
    host, _store, _seen = wire_server
    p = DavPath(f"dav://{host}/locked/")
    with pytest.raises(PermissionError) as excinfo:
        p.rm(recursive=True)
    assert "held file.txt" in str(excinfo.value)
    # The ignore_error policy still applies.
    p.rm(recursive=True, ignore_error=True)
    with pytest.raises(PermissionError):
        DavPath(f"dav://{host}/locked/f.txt")._delete()


def test_dav_207_move_with_locked_member_raises(wire_server):
    host, _store, _seen = wire_server
    with pytest.raises(PermissionError):
        DavPath(f"dav://{host}/locked/").rename("/elsewhere/")


# --- httpdav-dav-scandir-href-decoding ---


def test_dav_directory_with_space_lists_children_not_itself(dav_server, fixture_tree):
    (fixture_tree / "my dir").mkdir()
    (fixture_tree / "Team Docs" / "sub").mkdir(parents=True)
    (fixture_tree / "Team Docs" / "a.txt").write_text("a")
    (fixture_tree / "x#y.txt").write_text("hash")
    (fixture_tree / "café.txt").write_text("cafe")

    empty = DavPath(f"{dav_server}/my dir")
    assert list(empty.iterdir()) == []
    empty.rmdir()
    assert not (fixture_tree / "my dir").exists()

    team = DavPath(f"{dav_server}/Team Docs")
    assert sorted(c.name for c in team.iterdir()) == ["a.txt", "sub"]
    tops = [(top.name, sorted(dirs)) for top, dirs, _files in team.walk()]
    assert tops == [("Team Docs", ["sub"]), ("sub", [])]

    root = {c.name: c for c in DavPath(f"{dav_server}/").iterdir()}
    assert "x#y.txt" in root and "café.txt" in root
    assert root["x#y.txt"].read_text() == "hash"
    assert root["café.txt"].read_text() == "cafe"
