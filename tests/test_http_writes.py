"""What `http:` and `dav:` writes put on the wire, against a loopback server
that records every request it receives and keeps the files it was sent."""

import gc
import http.server
import re
import threading
import time
import typing

import pytest

requests = pytest.importorskip("requests")

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.dav import DavPath, _DavWriteStream
from pathlib_next.uri.schemes.http import HttpAppendStream, HttpPath, HttpWriteStream

# Short enough for a stalled reply to time out quickly, long enough for a
# loopback connect.
TIMEOUT = (5, 0.3)


class _Request(typing.NamedTuple):
    method: str
    path: str
    headers: dict
    body: bytes


_MULTISTATUS = (
    '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response>'
    "<D:href>{href}</D:href><D:propstat><D:prop><D:resourcetype>{kind}"
    "</D:resourcetype><D:getcontentlength>{size}</D:getcontentlength></D:prop>"
    "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>"
)


class _Wire:
    """Keeps `files` (path -> bytes) and `dirs`, logs every request in `log`
    and answers it like a small WebDAV server. `on(method, path, reply)`
    replaces the answer to one request: `reply` is `(status, headers, body)`
    or a callable taking the request handler (it returns such a tuple, or
    `None` after writing the response itself). `/r<status>/<name>` answers
    every method with that status and `Location: /store/<name>`."""

    def __init__(self):
        self.log: "list[_Request]" = []
        self.files: "dict[str, bytes]" = {}
        self.dirs: "set[str]" = set()
        self.rules: dict = {}
        self.port = None

    def on(self, method, path, reply):
        self.rules[(method, path)] = reply

    def sent(self, *methods):
        return [r for r in self.log if r.method in methods]

    def default(self, method, path, headers, body):
        redirect = re.match(r"/r(\d{3})/(.*)$", path)
        if redirect:
            return int(redirect[1]), {"Location": f"/store/{redirect[2]}"}, b""
        if method in ("GET", "HEAD"):
            if path in self.files:
                return (
                    200,
                    {"Content-Type": "application/octet-stream"},
                    self.files[path],
                )
            return 404, {}, b""
        if method == "PUT":
            if headers.get("If-None-Match") == "*" and path in self.files:
                return 412, {}, b""
            self.files[path] = body
            return 201, {}, b""
        if method == "PATCH":
            start = int(re.match(r"bytes (\d+)-", headers["Content-Range"])[1])
            self.files[path] = self.files.get(path, b"")[:start] + body
            return 204, {}, b""
        if method == "DELETE":
            if self.files.pop(path, None) is None and path not in self.dirs:
                return 404, {}, b""
            self.dirs.discard(path)
            return 204, {}, b""
        if method == "MKCOL":
            if path in self.files or path in self.dirs:
                return 405, {}, b""
            self.dirs.add(path)
            return 201, {}, b""
        if method == "MOVE":
            destination = re.sub(r"^https?://[^/]+", "", headers["Destination"])
            self.files[destination] = self.files.pop(path)
            return 201, {}, b""
        if method == "PROPFIND":
            if path in self.files or path in self.dirs:
                text = _MULTISTATUS.format(
                    href=path,
                    kind="<D:collection/>" if path in self.dirs else "",
                    size=len(self.files.get(path, b"")),
                )
                return 207, {"Content-Type": "application/xml"}, text.encode()
            return 404, {}, b""
        return 405, {}, b""


def _handler_class(wire):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = dict(self.headers.items())
            wire.log.append(_Request(self.command, self.path, headers, body))
            reply = wire.rules.get((self.command, self.path))
            if reply is None:
                reply = wire.default(self.command, self.path, headers, body)
            if callable(reply):
                reply = reply(self)
                if reply is None:
                    return
            status, extra, payload = reply
            self.send_response(status)
            extra = {"Content-Length": str(len(payload)), **extra}
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD" and payload:
                self.wfile.write(payload)

        do_GET = do_HEAD = do_PUT = do_PATCH = do_DELETE = _serve
        do_MKCOL = do_MOVE = do_PROPFIND = _serve

    return Handler


@pytest.fixture
def wire(serve_http):
    wire = _Wire()
    with serve_http(_handler_class(wire)) as base:
        wire.base = base
        wire.port = int(base.rsplit(":", 1)[1])
        yield wire


def _path(wire, scheme, name, **session_args):
    """`name` on the wire server as an `http:` or `dav:` path whose requests
    time out after `TIMEOUT`."""
    url = f"{scheme}{wire.base[len('http'):]}{name}"
    return UriPath(url).with_session(
        requests.Session(), **{"timeout": TIMEOUT, **session_args}
    )


def _stall(handler):
    """Answer nothing until the client has given up."""
    time.sleep(TIMEOUT[1] * 3)


def _cut_short(handler):
    """Promise 100 bytes, send 4, drop the connection."""
    handler.send_response(200)
    handler.send_header("Content-Length", "100")
    handler.end_headers()
    handler.wfile.write(b"part")
    handler.wfile.flush()
    handler.close_connection = True


def _failed_open(path, mode, error):
    """`path.open(mode)` must raise `error`. Returns once the failed call's
    frames are gone and a collection has run, so any finalizer of the
    half-built stream has already acted."""
    try:
        path.open(mode)
    except error:
        pass
    else:
        pytest.fail(f"open({mode!r}) returned a stream")
    gc.collect()


# --- a stream whose constructor did not finish sends nothing ---

_READ_FAILURES = {
    "500": ((500, {}, b""), OSError),
    "403": ((403, {}, b""), PermissionError),
    "timeout": (_stall, TimeoutError),
    "cut-short": (_cut_short, OSError),
}


@pytest.mark.parametrize("failure", _READ_FAILURES)
def test_append_open_whose_read_fails_sends_no_upload(wire, failure):
    reply, error = _READ_FAILURES[failure]
    wire.files["/log.txt"] = b"line1\nline2\n"
    wire.on("GET", "/log.txt", reply)
    _failed_open(_path(wire, "http", "/log.txt"), "ab", error)
    assert [r.method for r in wire.log] == ["GET"]
    assert wire.files["/log.txt"] == b"line1\nline2\n"


@pytest.mark.parametrize("failure", ["500", "403", "timeout"])
def test_patch_append_open_whose_size_probe_fails_sends_no_upload(wire, failure):
    reply, error = _READ_FAILURES[failure]
    wire.files["/log.txt"] = b"line1\nline2\n"
    wire.on("HEAD", "/log.txt", reply)
    path = _path(wire, "http", "/log.txt", append_mode="patch")
    _failed_open(path, "ab", error)
    assert [r.method for r in wire.log] == ["HEAD"]
    assert wire.files["/log.txt"] == b"line1\nline2\n"


@pytest.mark.parametrize("stream", [HttpWriteStream, HttpAppendStream, _DavWriteStream])
def test_stream_whose_constructor_never_ran_sends_nothing_on_close(wire, stream):
    unfinished = stream.__new__(stream)
    unfinished.write(b"half")
    unfinished.close()
    assert unfinished.closed
    unfinished = None
    gc.collect()
    assert wire.log == []


def test_finished_streams_still_upload_once_on_close(wire):
    for scheme, name in (("http", "/h.bin"), ("dav", "/d.bin")):
        stream = _path(wire, scheme, name).open("wb")
        stream.write(b"data")
        stream.close()
        stream.close()
        assert wire.files[name] == b"data"
    wire.files["/log.txt"] = b"one\n"
    with _path(wire, "http", "/log.txt").open("ab") as stream:
        stream.write(b"two\n")
    assert wire.files["/log.txt"] == b"one\ntwo\n"
    assert [r.method for r in wire.log].count("PUT") == 3
