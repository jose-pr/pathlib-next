"""What `http:` and `dav:` send for the credentials in a URL, which open modes
they accept, and which headers follow a redirect."""

import base64
import http.server
import pathlib
import threading

import pytest

requests = pytest.importorskip("requests")

import pathlib_next
from pathlib_next.uri import UriPath


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Logs `(method, path, headers)` of every request in `server.log` and
    answers 200 (207 with an empty multistatus for PROPFIND, 201 for the
    writes). `/redirect` answers 302 to `server.redirect_to`."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.log.append((self.command, self.path, dict(self.headers.items())))
        status, headers, body = 200, {}, b""
        if self.path == "/redirect":
            status, headers = 302, {"Location": self.server.redirect_to}
        elif self.command == "PROPFIND":
            status = 207
            body = (
                '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response>'
                "<D:href>/f.txt</D:href><D:propstat><D:prop><D:resourcetype/>"
                "<D:getcontentlength>3</D:getcontentlength></D:prop>"
                "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
                "</D:multistatus>"
            ).encode()
        elif self.command in ("PUT", "POST"):
            status = 201
        self.send_response(status)
        for key, value in {"Content-Length": str(len(body)), **headers}.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_HEAD = do_PUT = do_POST = do_PROPFIND = _serve


def _start():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.log, server.redirect_to = [], None
    server.base = f"127.0.0.1:{server.server_port}"
    server.thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    server.thread.start()
    return server


def _stop(server):
    server.shutdown()
    server.server_close()
    server.thread.join(timeout=5)


@pytest.fixture
def recorder():
    server = _start()
    try:
        yield server
    finally:
        _stop(server)


def _sent_credentials(server):
    for _method, _path, headers in server.log:
        value = {k.lower(): v for k, v in headers.items()}.get("authorization")
        if value:
            return base64.b64decode(value.split()[1])
    return None


@pytest.mark.parametrize("scheme", ["http", "dav"])
@pytest.mark.parametrize(
    "password, octets",
    [
        ("plain", b"plain"),
        ("p%40ss%3Aword", b"p@ss:word"),
        ("caf%C3%A9", b"caf\xc3\xa9"),
        ("%FF%FE", b"\xff\xfe"),
        ("%E9", b"\xe9"),
    ],
)
def test_url_password_is_sent_as_the_octets_it_stands_for(
    recorder, scheme, password, octets
):
    path = UriPath(f"{scheme}://us%40er:{password}@{recorder.base}/f.txt")
    assert path.stat().st_size in (0, 3)
    assert _sent_credentials(recorder) == b"us@er:" + octets


def test_ascii_credentials_are_still_passed_to_requests_as_text(monkeypatch):
    seen = []

    def request(self, method, url, **kwargs):
        seen.append(kwargs.get("auth"))
        raise requests.exceptions.ConnectionError("stop")

    monkeypatch.setattr(requests.Session, "request", request)
    UriPath("http://al%40ice:s3cr3t@h/x").exists()
    assert seen == [("al@ice", "s3cr3t")]


# --- an open mode is matched exactly ---


@pytest.mark.parametrize("scheme", ["http", "dav"])
@pytest.mark.parametrize("mode", ["r+", "r+b", "rb+", "w+", "a+"])
def test_read_write_modes_are_refused_before_any_request(recorder, scheme, mode):
    path = UriPath(f"{scheme}://{recorder.base}/f.txt")
    with pytest.raises(NotImplementedError):
        path.open(mode)
    assert recorder.log == []


@pytest.mark.parametrize("scheme", ["http", "dav"])
@pytest.mark.parametrize("mode", ["r", "rb", "rt"])
def test_plain_read_modes_still_open(recorder, scheme, mode):
    path = UriPath(f"{scheme}://{recorder.base}/f.txt")
    with path.open(mode) as handle:
        assert handle.read() in (b"", "")
    assert [m for m, _p, _h in recorder.log][0] == "GET"


# --- DavPath honours what it is given ---


def test_dav_write_method_is_the_method_that_uploads(recorder):
    path = UriPath(f"dav://{recorder.base}/f.txt").with_session(
        requests.Session(), write_method="POST"
    )
    path.write_bytes(b"data")
    assert [m for m, _p, _h in recorder.log] == ["POST"]


def test_dav_default_upload_is_still_put(recorder):
    UriPath(f"dav://{recorder.base}/f.txt").write_bytes(b"data")
    assert [m for m, _p, _h in recorder.log] == ["PUT"]


def test_dav_stat_accepts_the_keyword_of_its_base(recorder):
    path = UriPath(f"dav://{recorder.base}/f.txt")
    assert path.stat(walk_up_last_modified=True).st_size == 3
    assert [m for m, _p, _h in recorder.log] == ["PROPFIND"]


# --- headers and a redirect ---


def test_headers_other_than_authorization_follow_a_redirect(recorder):
    other = _start()
    try:
        # Another port is another origin to `requests`: it drops `Authorization`.
        recorder.redirect_to = f"http://127.0.0.1:{other.server_port}/landing"
        path = UriPath(f"http://{recorder.base}/redirect").with_session(
            requests.Session(),
            headers={"Authorization": "Bearer T", "X-Api-Key": "K"},
        )
        path.read_bytes()
    finally:
        _stop(other)
    ((_m, _p, first),) = recorder.log
    assert first["Authorization"] == "Bearer T" and first["X-Api-Key"] == "K"
    ((_m, landing, second),) = other.log
    assert landing == "/landing"
    assert "Authorization" not in second and second["X-Api-Key"] == "K"


def test_the_header_and_guide_say_which_headers_follow_a_redirect():
    header = (pathlib.Path(pathlib_next.__file__).parent / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    assert "other than `Authorization`" in header
    guide = pathlib.Path(__file__).parent.parent / "docs" / "guides" / "schemes.md"
    if guide.exists():
        assert "other than `Authorization`" in guide.read_text(encoding="utf-8")
