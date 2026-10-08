"""What `http:` and `dav:` listings and stats do with replies a server can get
wrong: a body that is cut short, stalls or is too large; hrefs that are not
members; sizes that are not numbers; names that are not UTF-8."""

import errno
import http.server
import time
import typing

import pytest

requests = pytest.importorskip("requests")

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes import http as http_module

TIMEOUT = (5, 30)
STALL_TIMEOUT = (5, 0.3)


class _Request(typing.NamedTuple):
    method: str
    path: str
    headers: dict
    body: bytes


class _Site:
    """A loopback server that logs every request in `log` and answers it from
    `routes`: `(method, path)` maps to `(status, headers, body)` or to a
    callable taking the request handler (it writes the response itself and
    returns `None`, or returns such a tuple). Anything else is a 404."""

    def __init__(self):
        self.log: "list[_Request]" = []
        self.routes: dict = {}

    def on(self, method, path, reply):
        self.routes[(method, path)] = reply

    def sent(self, *methods):
        return [r for r in self.log if not methods or r.method in methods]


def _handler_class(site):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def handle(self):
            # A client that gave up (a cut, refused or capped read) is the
            # point of several tests, not a server error.
            try:
                super().handle()
            except OSError:
                pass

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            site.log.append(
                _Request(self.command, self.path, dict(self.headers.items()), body)
            )
            reply = site.routes.get((self.command, self.path), (404, {}, b""))
            if callable(reply):
                reply = reply(self)
                if reply is None:
                    return
            status, extra, payload = reply
            self.send_response(status)
            if "Transfer-Encoding" not in extra:
                extra = {"Content-Length": str(len(payload)), **extra}
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD" and payload:
                self.wfile.write(payload)

        do_GET = do_HEAD = do_PROPFIND = _serve

    return Handler


@pytest.fixture
def site(serve_http):
    site = _Site()
    with serve_http(_handler_class(site)) as base:
        site.base = base
        yield site


def _http(site, name, **session_args):
    return UriPath(site.base + name).with_session(
        requests.Session(), **{"timeout": TIMEOUT, **session_args}
    )


def _dav(site, name, **session_args):
    return UriPath("dav" + site.base[len("http") :] + name).with_session(
        requests.Session(), **{"timeout": TIMEOUT, **session_args}
    )


def _rows(count=100):
    return "".join(
        f'<a href="f{i}.txt">f{i}.txt</a> 07-Oct-2026 10:00 12\n' for i in range(count)
    )


INDEX = (
    "<html><head><title>Index of /d/</title></head><body><pre>"
    + _rows()
    + "</pre></body></html>"
).encode()


def _cut_short(handler):
    """Promise the whole index, send a quarter of it, drop the connection."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Content-Length", str(len(INDEX)))
    handler.end_headers()
    handler.wfile.write(INDEX[: len(INDEX) // 4])
    handler.wfile.flush()
    handler.close_connection = True


def _cut_mid_chunk(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    handler.wfile.write(b"10\r\n<html><body><pre\r\n")
    handler.wfile.flush()
    handler.close_connection = True


def _stalled(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Content-Length", "5000")
    handler.end_headers()
    handler.wfile.write(b"<html>")
    handler.wfile.flush()
    time.sleep(STALL_TIMEOUT[1] * 4)


# --- a listing body that does not arrive whole is an error ---


@pytest.mark.parametrize("cut", [_cut_short, _cut_mid_chunk], ids=["short", "chunk"])
def test_index_cut_short_raises_instead_of_listing_part_of_it(site, cut):
    site.on("GET", "/d/", cut)
    with pytest.raises(OSError) as raised:
        list(_http(site, "/d/").iterdir())
    # Not the transport's exception, and naming the directory.
    assert not isinstance(raised.value, requests.exceptions.RequestException)
    assert raised.value.errno == errno.EIO
    assert site.base + "/d/" in str(raised.value)


def test_stalled_index_is_a_timeout(site):
    site.on("GET", "/d/", _stalled)
    with pytest.raises(TimeoutError):
        list(_http(site, "/d/", timeout=STALL_TIMEOUT).iterdir())


def test_undecodable_index_body_is_an_oserror_naming_the_path(site):
    site.on(
        "GET",
        "/d/",
        (200, {"Content-Type": "text/html", "Content-Encoding": "gzip"}, b"not gzip"),
    )
    with pytest.raises(OSError) as raised:
        list(_http(site, "/d/").iterdir())
    assert not isinstance(raised.value, requests.exceptions.RequestException)
    assert raised.value.errno == errno.EIO
    assert site.base + "/d/" in str(raised.value)


# --- a listing body is capped ---


def test_listing_cap_is_a_documented_module_constant():
    assert http_module.MAX_LISTING_BYTES == 8 * 1024 * 1024


def test_index_over_the_cap_raises_before_it_is_read(site, monkeypatch):
    monkeypatch.setattr(http_module, "MAX_LISTING_BYTES", len(INDEX) - 1)
    site.on("GET", "/d/", (200, {"Content-Type": "text/html"}, INDEX))
    with pytest.raises(OSError) as raised:
        list(_http(site, "/d/").iterdir())
    assert raised.value.errno == errno.EFBIG
    assert site.base + "/d/" in str(raised.value)


def test_unsized_index_over_the_cap_raises_while_reading(site, monkeypatch):
    monkeypatch.setattr(http_module, "MAX_LISTING_BYTES", 1000)

    def chunked(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        for _ in range(20):
            handler.wfile.write(b"100\r\n" + b"x" * 256 + b"\r\n")
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()

    site.on("GET", "/d/", chunked)
    with pytest.raises(OSError) as raised:
        list(_http(site, "/d/").iterdir())
    assert raised.value.errno == errno.EFBIG


def test_index_of_exactly_the_cap_is_listed(site, monkeypatch):
    monkeypatch.setattr(http_module, "MAX_LISTING_BYTES", len(INDEX))
    site.on("GET", "/d/", (200, {"Content-Type": "text/html"}, INDEX))
    names = [p.name for p in _http(site, "/d/").iterdir()]
    assert len(names) == 100


# --- a directory is told from the path of its URL ---


@pytest.mark.parametrize("suffix", ["?C=M", "#top", "?a=1#top"])
def test_directory_url_with_query_or_fragment_is_a_directory(http_server, suffix):
    assert UriPath(f"{http_server}/sub/{suffix}").is_dir()


def test_unlink_refuses_a_directory_url_that_carries_a_query(
    http_writable_server, fixture_tree
):
    with pytest.raises(IsADirectoryError):
        UriPath(f"{http_writable_server}/sub/?x=1").unlink()
    assert (fixture_tree / "sub" / "c.py").exists()


# --- sizes a server states ---


@pytest.mark.parametrize(
    "length",
    ["abc", "-5", "1e3", "", "9" * 5000],
    ids=["word", "negative", "exponent", "empty", "huge"],
)
def test_http_content_length_that_is_no_size_is_unknown(site, length):
    def reply(handler):
        # Written by hand: the stdlib handler would not send a bad length.
        head = f"HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n\r\n"
        handler.wfile.write(head.encode())
        handler.close_connection = True

    site.on("HEAD", "/f.bin", reply)
    stat = _http(site, "/f.bin").stat()
    assert stat.st_size == 0 and not stat.is_dir()


def test_http_stat_still_reads_a_stated_size(site):
    site.on("HEAD", "/f.bin", (200, {"Content-Length": "12"}, b""))
    assert _http(site, "/f.bin").stat().st_size == 12


def _multistatus(*responses):
    return (
        '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
        + "".join(responses)
        + "</D:multistatus>"
    ).encode()


def _response(href, collection=False, length="4", extra=""):
    kind = "<D:collection/>" if collection else ""
    return (
        f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
        f"<D:resourcetype>{kind}</D:resourcetype>"
        f"<D:getcontentlength>{length}</D:getcontentlength>{extra}"
        "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    )


def _propfind(site, path, *responses, status=207):
    site.on("PROPFIND", path, (status, {}, _multistatus(*responses)))


@pytest.mark.parametrize("length", ["abc", "-5", "1e3"])
def test_dav_getcontentlength_that_is_no_size_is_unknown(site, length):
    _propfind(
        site,
        "/d/",
        _response("/d/", collection=True),
        _response("/d/x.txt", length=length),
        _response("/d/y.txt", length="7"),
    )
    sizes = {p.name: p.stat().st_size for p in _dav(site, "/d/").iterdir()}
    assert sizes == {"x.txt": 0, "y.txt": 7}
    _propfind(site, "/d/x.txt", _response("/d/x.txt", length=length))
    assert _dav(site, "/d/x.txt").stat().st_size == 0


# --- names that are not UTF-8 ---

_LATIN1 = "caf%E9-latin1.txt"
_PAGES = {
    "pre": (
        '<html><head><title>Index of /d/</title></head><body><pre><a href="../">../</a>\n'
        f'<a href="{_LATIN1}">x</a>   07-Oct-2026 10:00   12\n</pre></body></html>'
    ),
    "table": (
        "<html><body><table><tr><th>Name</th><th>Last modified</th><th>Size</th></tr>"
        f'<tr><td><a href="{_LATIN1}">x</a></td><td>2026-10-07 10:00</td><td>12</td></tr>'
        "</table></body></html>"
    ),
    "ul": f'<html><body><ul><li><a href="{_LATIN1}">x</a></li></ul></body></html>',
}


@pytest.mark.parametrize("style", sorted(_PAGES))
def test_http_listed_name_that_is_not_utf8_is_requested_as_listed(site, style):
    site.on("GET", "/d/", (200, {"Content-Type": "text/html"}, _PAGES[style].encode()))
    site.on("GET", "/d/" + _LATIN1, (200, {}, b"data"))
    (child,) = _http(site, "/d/").iterdir()
    assert child.name == "caf\udce9-latin1.txt"
    assert child.read_bytes() == b"data"
    assert site.sent("GET")[-1].path == "/d/" + _LATIN1


def test_dav_listed_name_that_is_not_utf8_is_requested_as_listed(site):
    _propfind(
        site,
        "/d/",
        _response("/d/", collection=True),
        _response("/d/" + _LATIN1),
    )
    site.on("GET", "/d/" + _LATIN1, (200, {}, b"data"))
    (child,) = _dav(site, "/d/").iterdir()
    assert child.name == "caf\udce9-latin1.txt"
    assert child.read_bytes() == b"data"
    assert site.sent("GET")[-1].path == "/d/" + _LATIN1


# --- a subdirectory is listed with one request ---


def test_listing_a_directory_without_a_slash_asks_for_the_slash_form(
    http_server, monkeypatch
):
    # The stdlib server answers `/sub` with a redirect to `/sub/`.
    seen = []
    real = requests.Session.request

    def spy(self, method, url, *args, **kwargs):
        seen.append((method, url[len(http_server) :]))
        return real(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", spy)
    names = sorted(p.name for p in UriPath(f"{http_server}/sub").iterdir())
    assert names == ["c.py", "nested"]
    assert seen == [("GET", "/sub/")]


def test_listing_a_file_still_raises_not_a_directory(http_server):
    with pytest.raises(NotADirectoryError):
        list(UriPath(f"{http_server}/a.txt").iterdir())


def test_listing_a_missing_path_raises_file_not_found(http_server):
    with pytest.raises(FileNotFoundError):
        list(UriPath(f"{http_server}/nothing").iterdir())


# --- stat follows the Location of a redirect once ---


def test_stat_of_a_redirecting_directory_follows_the_location_once(site):
    site.on("HEAD", "/a", (301, {"Location": "/a/"}, b""))
    site.on("HEAD", "/a/", (200, {"Content-Length": "0"}, b""))
    assert _http(site, "/a").stat().is_dir()
    assert [(r.method, r.path) for r in site.log] == [("HEAD", "/a"), ("HEAD", "/a/")]


def test_stat_keeps_the_credentials_through_a_same_origin_redirect(site):
    site.on("HEAD", "/a", (301, {"Location": "/a/"}, b""))
    site.on("HEAD", "/a/", (200, {"Content-Length": "0"}, b""))
    host = site.base[len("http://") :]
    assert UriPath(f"http://user:pw@{host}/a").stat().is_dir()
    assert [r.path for r in site.log] == ["/a", "/a/"]
    assert all(
        r.headers.get("Authorization", "").startswith("Basic ") for r in site.log
    )


def test_stat_follows_an_absolute_location_on_the_same_origin(site):
    site.on("HEAD", "/a", (302, {"Location": site.base + "/b.bin"}, b""))
    site.on("HEAD", "/b.bin", (200, {"Content-Length": "9"}, b""))
    assert _http(site, "/a").stat().st_size == 9


# --- a DAV reply names the collection and its direct members only ---


def test_dav_listing_keeps_only_direct_members_of_the_collection(site):
    _propfind(
        site,
        "/d/",
        _response("/d/", collection=True),
        _response("/d/ok.txt"),
        _response("/d/sub/", collection=True),
        _response("/d/deep/er.txt"),
        _response("/elsewhere/outside.txt"),
        _response("http://evil.example/d/foreign.txt"),
        _response("//evil.example/d/netpath.txt"),
        _response("/d/../up.txt"),
        _response("/D/case.txt"),
        _response("/d/a%2Fb.txt"),
        _response("/d/%2e%2e"),
    )
    names = sorted(p.name for p in _dav(site, "/d/").iterdir())
    assert names == ["ok.txt", "sub"]


def test_dav_listing_resolves_relative_and_absolute_hrefs_of_the_same_host(site):
    host = site.base[len("http://") :]
    _propfind(
        site,
        "/d/",
        _response("/d/", collection=True),
        _response("rel.txt"),
        _response(f"http://{host}/d/abs.txt"),
        _response(f"https://{host}/d/upgraded.txt"),
    )
    names = sorted(p.name for p in _dav(site, "/d/").iterdir())
    assert names == ["abs.txt", "rel.txt", "upgraded.txt"]


def test_dav_listing_of_a_collection_whose_own_href_is_spelled_differently(site):
    # A reverse proxy that strips a prefix: nothing in the reply is the
    # collection or a member of it.
    _propfind(
        site,
        "/d/",
        _response("/backend/d/", collection=True),
        _response("/backend/d/x.txt"),
    )
    with pytest.raises(OSError) as raised:
        list(_dav(site, "/d/").iterdir())
    assert raised.value.errno == errno.EIO


@pytest.mark.parametrize("odd", ["", "/", "   "])
def test_dav_member_with_an_empty_or_root_href_does_not_fail_the_listing(site, odd):
    _propfind(
        site,
        "/d/",
        _response("/d/", collection=True),
        _response("/d/ok.txt"),
        _response(odd),
    )
    assert [p.name for p in _dav(site, "/d/").iterdir()] == ["ok.txt"]


def test_dav_listing_without_the_collection_entry_still_lists_members(site):
    _propfind(site, "/d/", _response("/d/x.txt"))
    assert [p.name for p in _dav(site, "/d/").iterdir()] == ["x.txt"]


def test_dav_listing_of_a_directory_with_a_space_and_of_the_root(site):
    _propfind(
        site,
        "/my%20dir/",
        _response("/my%20dir/", collection=True),
        _response("/my%20dir/a%20b.txt"),
    )
    assert [p.name for p in _dav(site, "/my dir/").iterdir()] == ["a b.txt"]
    _propfind(site, "/", _response("/", collection=True), _response("/top.txt"))
    assert [p.name for p in _dav(site, "/").iterdir()] == ["top.txt"]


def test_dav_file_listed_as_a_directory_is_not_a_directory(site):
    _propfind(site, "/f.txt", _response("/f.txt"))
    with pytest.raises(NotADirectoryError):
        list(_dav(site, "/f.txt").iterdir())


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><other/>',
        b"<html><body>login</body></html>",
        b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"/>',
        b"",
    ],
    ids=["other-xml", "html", "xhtml", "empty"],
)
@pytest.mark.parametrize("status", [200, 207])
def test_dav_reply_that_is_not_a_multistatus_is_an_error(site, body, status):
    site.on("PROPFIND", "/d/", (status, {}, body))
    path = _dav(site, "/d/")
    for action in (path.stat, lambda: list(path.iterdir())):
        with pytest.raises(OSError) as raised:
            action()
        assert raised.value.errno == errno.EIO
        assert not isinstance(raised.value, FileNotFoundError)
    assert path.exists() is False


# --- a redirected PROPFIND is sent again, and only to the same origin ---

_STORED = _response("/store/f.bin", length="5")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_propfind_behind_a_redirect_is_sent_again_with_its_body(site, status):
    site.on("PROPFIND", "/r/f.bin", (status, {"Location": "/store/f.bin"}, b""))
    _propfind(site, "/store/f.bin", _STORED)
    assert _dav(site, "/r/f.bin").stat().st_size == 5
    first, second = site.sent("PROPFIND")
    assert (first.path, second.path) == ("/r/f.bin", "/store/f.bin")
    assert second.body == first.body and second.body
    assert second.headers["Depth"] == first.headers["Depth"] == "0"
    assert [r.method for r in site.log] == ["PROPFIND", "PROPFIND"]


def test_listing_behind_a_redirect_is_sent_again_at_depth_one(site):
    site.on("PROPFIND", "/old/", (301, {"Location": "/new/"}, b""))
    _propfind(site, "/new/", _response("/new/", collection=True), _response("/new/x"))
    assert [p.name for p in _dav(site, "/old/").iterdir()] == ["x"]
    assert {r.headers["Depth"] for r in site.sent("PROPFIND")} == {"1"}


def test_propfind_redirect_to_another_origin_is_refused_and_not_sent(site, serve_http):
    other = _Site()
    with serve_http(_handler_class(other)) as other_base:
        site.on("PROPFIND", "/r/f.bin", (301, {"Location": other_base + "/f.bin"}, b""))
        with pytest.raises(OSError) as raised:
            _dav(site, "/r/f.bin").stat()
    assert raised.value.errno == errno.EIO
    assert "301" in str(raised.value) and other_base + "/f.bin" in str(raised.value)
    assert other.log == []


def test_propfind_redirect_loop_is_refused_after_one_more_request(site):
    site.on("PROPFIND", "/a", (302, {"Location": "/b"}, b""))
    site.on("PROPFIND", "/b", (302, {"Location": "/a"}, b""))
    with pytest.raises(OSError) as raised:
        _dav(site, "/a").stat()
    assert raised.value.errno == errno.EIO
    assert [r.path for r in site.log] == ["/a", "/b"]


def test_get_still_follows_a_redirect_as_requests_does(site):
    site.on("GET", "/r/f.bin", (302, {"Location": "/store/f.bin"}, b""))
    site.on("GET", "/store/f.bin", (200, {"Content-Type": "text/plain"}, b"hello"))
    assert _dav(site, "/r/f.bin").read_bytes() == b"hello"
    assert _http(site, "/r/f.bin").read_bytes() == b"hello"
