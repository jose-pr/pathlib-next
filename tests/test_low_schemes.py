"""Scheme soundness edges: nested archive addressing, HTTP listing scope and
iterdir() on a file, HTTP/WebDAV status translation, and WebDAV multistatus
propstat groups. Every server is an in-process loopback handler."""

import errno
import http.server
import importlib.util
import io
import tarfile
import zipfile

import pytest

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.archive import _split_archive_path

# --- ftparchive-nested-archives ------------------------------------------------


@pytest.fixture
def nested_zip(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("x.txt", b"inner-x")
        archive.writestr("d!/y.txt", b"bang")
    tar_inner = io.BytesIO()
    with tarfile.open(fileobj=tar_inner, mode="w") as archive:
        info = tarfile.TarInfo("t.txt")
        info.size = 3
        archive.addfile(info, io.BytesIO(b"tar"))
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("inner.zip", inner.getvalue())
        archive.writestr("lib/inner.tar", tar_inner.getvalue())
        archive.writestr("top.txt", b"top")
    return outer


@pytest.mark.parametrize(
    "path, archive, inner",
    [
        (
            "zip:file:///o.zip!/inner.zip!/x.txt",
            "zip:file:///o.zip!/inner.zip",
            "x.txt",
        ),
        ("zip:file:///o.zip!/inner.zip!/", "zip:file:///o.zip!/inner.zip", ""),
        ("zip:file:///o.zip!/inner.zip!", "zip:file:///o.zip!/inner.zip", ""),
        (
            "archive+zip:tar:file:///o.tar!/a.tar!/i.zip!/x",
            "archive+zip:tar:file:///o.tar!/a.tar!/i.zip",
            "x",
        ),
        # Not nested: the first separator, as before.
        ("file:///a.zip!/dir!/x", "file:///a.zip", "dir!/x"),
        ("http://h/zip:x.zip!/m", "http://h/zip:x.zip", "m"),
    ],
)
def test_split_counts_one_separator_per_nested_archive_scheme(path, archive, inner):
    assert _split_archive_path(path) == (archive, inner)


def test_nested_zip_member_reads(nested_zip):
    p = UriPath(f"zip:zip:{nested_zip.as_uri()}!/inner.zip!/x.txt")
    assert p.read_bytes() == b"inner-x"
    assert p.path == "x.txt"


def test_nested_uri_round_trips_through_as_uri(nested_zip):
    member = UriPath(f"zip:{nested_zip.as_uri()}!/inner.zip")
    p = UriPath(f"zip:{member.as_uri()}!/x.txt")
    assert p.read_bytes() == b"inner-x"
    assert UriPath(p.as_uri()).read_bytes() == b"inner-x"


def test_nested_archive_lists_and_escapes_bang_slash_names(nested_zip):
    root = UriPath(f"zip:zip:{nested_zip.as_uri()}!/inner.zip!/")
    assert sorted(c.name for c in root.iterdir()) == ["d!", "x.txt"]
    y = root / "d!" / "y.txt"
    assert "d%21/y.txt" in y.as_uri()
    assert y.read_bytes() == b"bang"
    assert UriPath(y.as_uri()).read_bytes() == b"bang"


def test_single_level_member_with_bang_slash_round_trips(tmp_path):
    outer = tmp_path / "a.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("d!/y.txt", b"bang")
    y = UriPath(f"zip:{outer.as_uri()}!/d!/y.txt")
    assert y.read_bytes() == b"bang"
    assert UriPath(y.as_uri()).read_bytes() == b"bang"


def test_nested_tar_inside_zip_and_autodetect(nested_zip):
    t = UriPath(f"tar:zip:{nested_zip.as_uri()}!/lib/inner.tar!/t.txt")
    assert t.read_bytes() == b"tar"
    detected = UriPath(f"archive:zip:{nested_zip.as_uri()}!/inner.zip!/x.txt")
    assert detected.read_bytes() == b"inner-x"


def test_nested_archive_is_read_only(nested_zip):
    p = UriPath(f"zip:zip:{nested_zip.as_uri()}!/inner.zip!/new.txt")
    with pytest.raises(NotImplementedError):
        p.write_bytes(b"no")


def test_outer_archive_still_reads(nested_zip):
    assert UriPath(f"zip:{nested_zip.as_uri()}!/top.txt").read_bytes() == b"top"


# --- HTTP ------------------------------------------------------------------


#: The HTTP/WebDAV tests need the `http` extra; the archive tests above do not.
needs_http = pytest.mark.skipif(
    importlib.util.find_spec("requests") is None, reason="requires requests"
)


def _handler(routes, **methods):
    """A handler answering GET from `routes` (path -> (status, headers,
    body)) and any other method from `methods` (name -> callable(handler))."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = routes.get(
                self.path.split("?", 1)[0], (404, {}, b"")
            )
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    for name, fn in methods.items():
        setattr(_Handler, f"do_{name}", fn)
    return _Handler


# --- httpdav-iterdir-on-a-file ------------------------------------------------


@needs_http
def test_iterdir_on_a_non_html_file_raises_without_reading_it(serve_http, monkeypatch):
    body = b"<a href='x'>x</a>" * 20000
    routes = {"/big.bin": (200, {"Content-Type": "application/octet-stream"}, body)}
    import requests

    streamed = []
    real = requests.Session.request

    def request(self, method, url, **kwargs):
        streamed.append(kwargs.get("stream"))
        return real(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", request)
    with serve_http(_handler(routes)) as base:
        with pytest.raises(NotADirectoryError) as excinfo:
            list(UriPath(f"{base}/big.bin").iterdir())
    assert excinfo.value.errno == errno.ENOTDIR
    assert excinfo.value.filename == f"{base}/big.bin"
    assert streamed == [True]


@needs_http
def test_iterdir_on_a_text_file_of_the_real_server_raises(http_server):
    with pytest.raises(NotADirectoryError):
        list(UriPath(f"{http_server}/a.txt").iterdir())
    assert sorted(c.name for c in UriPath(f"{http_server}/sub/").iterdir()) == [
        "c.py",
        "nested",
    ]


@needs_http
def test_html_page_links_to_other_hosts_are_not_children(serve_http):
    page = (
        b"<html><body>"
        b'<a href="https://cdn.example/lib.js">lib</a>'
        b'<a href="//cdn.example/other.js">other</a>'
        b'<a href="about.html">about</a>'
        b"</body></html>"
    )
    routes = {"/page.html": (200, {"Content-Type": "text/html"}, page)}
    with serve_http(_handler(routes)) as base:
        assert list(UriPath(f"{base}/page.html").iterdir()) == []


# --- httpdav-listing-scope-uses-title -------------------------------------------


@needs_http
def test_listing_behind_a_prefix_is_scoped_by_the_request_url(serve_http):
    listing = (
        b"<html><head><title>Index of /pub</title></head><body><pre>"
        b'<a href="/mirror/">Parent Directory</a>\n'
        b'<a href="/mirror/pub/a.txt">a.txt</a>  11-Jul-2026 10:23  5\n'
        b'<a href="/mirror/pub/sub/">sub/</a>  11-Jul-2026 10:23  -\n'
        b'<a href="/mirror/pub/sub/deep.txt">deep.txt</a>  11-Jul-2026 10:23  5\n'
        b'<a href="https://elsewhere.example/b/tracker.js">t</a>\n'
        b"</pre></body></html>"
    )
    routes = {"/mirror/pub/": (200, {"Content-Type": "text/html"}, listing)}
    with serve_http(_handler(routes)) as base:
        children = {c.name: c for c in UriPath(f"{base}/mirror/pub/").iterdir()}
    assert sorted(children) == ["a.txt", "sub"]
    assert children["sub"].is_dir()


@needs_http
def test_table_listing_skips_foreign_and_non_child_hrefs(serve_http):
    listing = (
        b"<html><body><table>"
        b"<tr><th>Name</th><th>Size</th></tr>"
        b'<tr><td><a href="a.txt">a.txt</a></td><td>1</td></tr>'
        b'<tr><td><a href="http://other.example/d/b.txt">b.txt</a></td><td>1</td></tr>'
        b'<tr><td><a href="../up.txt">up.txt</a></td><td>1</td></tr>'
        b"</table></body></html>"
    )
    routes = {"/d/": (200, {"Content-Type": "text/html; charset=utf-8"}, listing)}
    with serve_http(_handler(routes)) as base:
        assert [c.name for c in UriPath(f"{base}/d/").iterdir()] == ["a.txt"]


@needs_http
def test_parser_without_base_url_keeps_title_scoping():
    from pathlib_next.uri.schemes.http import _DirectoryListingParser

    parser = _DirectoryListingParser()
    parser.feed(
        "<html><head><title>Index of /pub</title></head><body><pre>"
        '<a href="/pub/a.txt">a.txt</a>\n'
        '<a href="/">up</a>\n'
        "</pre></body></html>"
    )
    parser.close()
    assert [e.name for e in parser.listing] == ["a.txt"]


# --- httpdav-409-mapped-to-fileexistserror ----------------------------------


def _reply(status):
    def handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    return handle


@needs_http
def test_put_409_missing_parent_is_file_not_found(serve_http):
    with serve_http(_handler({}, PUT=_reply(409))) as base:
        p = UriPath(f"{base}/nodir/f.txt")
        with pytest.raises(FileNotFoundError) as excinfo:
            p.write_bytes(b"x")
    assert excinfo.value.errno == errno.ENOENT
    assert excinfo.value.filename == str(p)


@needs_http
def test_410_gone_is_file_not_found_and_errors_carry_errno(serve_http):
    routes = {"/gone": (410, {}, b""), "/secret": (403, {}, b"")}
    with serve_http(_handler(routes)) as base:
        with pytest.raises(FileNotFoundError) as gone:
            UriPath(f"{base}/gone").read_bytes()
        with pytest.raises(FileNotFoundError) as missing:
            UriPath(f"{base}/nothing").read_bytes()
        with pytest.raises(PermissionError) as denied:
            UriPath(f"{base}/secret").read_bytes()
    assert gone.value.errno == missing.value.errno == errno.ENOENT
    assert missing.value.filename == f"{base}/nothing"
    assert denied.value.errno == errno.EACCES


@needs_http
def test_dav_statuses_carry_errno_and_filename(serve_http):
    from pathlib_next.uri.schemes.dav import DavPath

    with serve_http(_handler({}, MKCOL=_reply(409))) as base:
        p = DavPath(base.replace("http:", "dav:") + "/nodir/new")
        with pytest.raises(FileNotFoundError) as excinfo:
            p.mkdir()
    assert excinfo.value.errno == errno.ENOENT
    assert excinfo.value.filename == str(p)


# --- httpdav-dav-propstat-first-only ----------------------------------------


_MULTISTATUS_404_FIRST = b"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/docs/</D:href>
    <D:propstat>
      <D:prop><D:getcontentlength/></D:prop>
      <D:status>HTTP/1.1 404 Not Found</D:status>
    </D:propstat>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/></D:resourcetype>
        <D:getlastmodified>Wed, 01 Jul 2026 10:00:00 GMT</D:getlastmodified>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/docs/f.txt</D:href>
    <D:propstat>
      <D:prop><D:resourcetype/></D:prop>
      <D:status>HTTP/1.1 404 Not Found</D:status>
    </D:propstat>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:getcontentlength>7</D:getcontentlength>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""


def _propfind(self):
    length = int(self.headers.get("Content-Length") or 0)
    self.rfile.read(length)
    body = _MULTISTATUS_404_FIRST
    if self.headers.get("Depth") == "0":
        # Only the collection's own response.
        start = body.index(b"  <D:response>\n    <D:href>/docs/f.txt")
        body = body[:start] + b"</D:multistatus>"
    self.send_response(207)
    self.send_header("Content-Type", "application/xml")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)


@needs_http
def test_dav_reads_every_successful_propstat(serve_http):
    from pathlib_next.uri.schemes.dav import DavPath

    with serve_http(_handler({}, PROPFIND=_propfind)) as base:
        docs = DavPath(base.replace("http:", "dav:") + "/docs/")
        st = docs.stat()
        assert st.is_dir()
        assert st.st_mtime > 0
        listing = dict(docs._scandir())
    assert list(listing) == ["f.txt"]
    assert not listing["f.txt"].is_dir()
    assert listing["f.txt"].st_size == 7
