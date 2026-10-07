"""What `http:` and `dav:` writes put on the wire, against a loopback server
that records every request it receives and keeps the files it was sent."""

import errno
import gc
import http.server
import io
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
            if "Transfer-Encoding" not in extra:
                extra = {"Content-Length": str(len(payload)), **extra}
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD" and payload:
                self.wfile.write(payload)

        do_GET = do_HEAD = do_PUT = do_PATCH = do_DELETE = _serve
        do_MKCOL = do_MOVE = do_PROPFIND = do_POST = _serve

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


# --- redirects of state-changing requests ---

_SCHEMES = ["http", "dav"]
_NOT_RESENT = [301, 302, 303]
_RESENT = [307, 308]


def _refused(excinfo, status, location):
    error = excinfo.value
    assert error.errno == errno.EIO
    assert str(status) in str(error) and location in str(error)
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.parametrize("scheme", _SCHEMES)
@pytest.mark.parametrize("status", _NOT_RESENT)
def test_put_through_a_redirect_that_is_not_307_or_308_raises(wire, scheme, status):
    wire.files["/store/f.bin"] = b"ORIGINAL"
    path = _path(wire, scheme, f"/r{status}/f.bin")
    with pytest.raises(OSError) as excinfo:
        path.write_bytes(b"NEW-PAYLOAD")
    _refused(excinfo, status, f"{wire.base}/store/f.bin")
    assert [(r.method, r.path) for r in wire.log] == [("PUT", f"/r{status}/f.bin")]
    assert wire.files == {"/store/f.bin": b"ORIGINAL"}


@pytest.mark.parametrize("status", [300, 304])
def test_put_answered_with_any_other_3xx_raises(wire, status):
    wire.on("PUT", "/f.bin", (status, {}, b""))
    with pytest.raises(OSError) as excinfo:
        _path(wire, "http", "/f.bin").write_bytes(b"NEW")
    assert excinfo.value.errno == errno.EIO and str(status) in str(excinfo.value)
    assert wire.files == {}


@pytest.mark.parametrize("scheme", _SCHEMES)
@pytest.mark.parametrize("status", _RESENT)
@pytest.mark.parametrize("style", ["relative", "absolute"])
def test_put_through_307_or_308_sends_the_whole_body_to_the_target(
    wire, scheme, status, style
):
    wire.files["/store/f.bin"] = b"ORIGINAL"
    if style == "absolute":
        location = f"{wire.base}/store/f.bin"
        wire.on("PUT", "/moved/f.bin", (status, {"Location": location}, b""))
        name = "/moved/f.bin"
    else:
        name = f"/r{status}/f.bin"
    assert _path(wire, scheme, name).write_bytes(b"NEW-PAYLOAD") == 11
    assert [(r.method, r.path, r.body) for r in wire.log] == [
        ("PUT", name, b"NEW-PAYLOAD"),
        ("PUT", "/store/f.bin", b"NEW-PAYLOAD"),
    ]
    assert wire.files["/store/f.bin"] == b"NEW-PAYLOAD"


@pytest.mark.parametrize("scheme", _SCHEMES)
@pytest.mark.parametrize("status", _NOT_RESENT)
def test_delete_through_a_redirect_that_is_not_307_or_308_raises(wire, scheme, status):
    wire.files["/store/f.bin"] = b"KEEP"
    with pytest.raises(OSError) as excinfo:
        _path(wire, scheme, f"/r{status}/f.bin")._delete()
    _refused(excinfo, status, f"{wire.base}/store/f.bin")
    assert [(r.method, r.path) for r in wire.log] == [("DELETE", f"/r{status}/f.bin")]
    assert wire.files == {"/store/f.bin": b"KEEP"}


@pytest.mark.parametrize("scheme", _SCHEMES)
@pytest.mark.parametrize("status", _RESENT)
def test_delete_through_307_or_308_deletes_the_target(wire, scheme, status):
    wire.files["/store/f.bin"] = b"GONE"
    _path(wire, scheme, f"/r{status}/f.bin")._delete()
    assert [(r.method, r.path) for r in wire.log] == [
        ("DELETE", f"/r{status}/f.bin"),
        ("DELETE", "/store/f.bin"),
    ]
    assert wire.files == {}


@pytest.mark.parametrize("status", _NOT_RESENT)
def test_dav_recursive_rm_through_a_redirect_that_is_not_307_or_308_raises(
    wire, status
):
    wire.files["/store/d"] = b"KEEP"
    with pytest.raises(OSError) as excinfo:
        _path(wire, "dav", f"/r{status}/d").rm(recursive=True)
    _refused(excinfo, status, f"{wire.base}/store/d")
    assert [r.method for r in wire.log] == ["DELETE"]
    assert wire.files == {"/store/d": b"KEEP"}


@pytest.mark.parametrize("status", _NOT_RESENT)
def test_dav_mkdir_through_a_redirect_that_is_not_307_or_308_raises(wire, status):
    with pytest.raises(OSError) as excinfo:
        _path(wire, "dav", f"/r{status}/newdir").mkdir()
    _refused(excinfo, status, f"{wire.base}/store/newdir")
    assert [(r.method, r.path) for r in wire.log] == [("MKCOL", f"/r{status}/newdir")]
    assert wire.dirs == set()


@pytest.mark.parametrize("status", _RESENT)
def test_dav_mkdir_through_307_or_308_creates_the_target(wire, status):
    _path(wire, "dav", f"/r{status}/newdir").mkdir()
    assert [(r.method, r.path) for r in wire.log] == [
        ("MKCOL", f"/r{status}/newdir"),
        ("MKCOL", "/store/newdir"),
    ]
    assert wire.dirs == {"/store/newdir"}


@pytest.mark.parametrize("status", _NOT_RESENT)
def test_dav_rename_through_a_redirect_that_is_not_307_or_308_raises(wire, status):
    wire.files["/store/f.bin"] = b"MOVED"
    source = _path(wire, "dav", f"/r{status}/f.bin")
    with pytest.raises(OSError) as excinfo:
        source.rename(source.with_path("/dest/g.bin"))
    _refused(excinfo, status, f"{wire.base}/store/f.bin")
    assert [r.method for r in wire.log] == ["MOVE"]
    assert wire.files == {"/store/f.bin": b"MOVED"}


@pytest.mark.parametrize("status", _RESENT)
def test_dav_rename_through_307_or_308_repeats_the_move_with_its_headers(wire, status):
    wire.files["/store/f.bin"] = b"MOVED"
    source = _path(wire, "dav", f"/r{status}/f.bin")
    source.rename(source.with_path("/dest/g.bin"))
    first, second = wire.log
    assert (first.method, second.method) == ("MOVE", "MOVE")
    assert second.path == "/store/f.bin"
    assert second.headers["Destination"] == first.headers["Destination"]
    assert second.headers["Overwrite"] == "F"
    assert wire.files == {"/dest/g.bin": b"MOVED"}


def test_rewrite_append_through_a_301_does_not_empty_the_file(wire):
    wire.files["/store/log.txt"] = b"one\n"
    with pytest.raises(OSError):
        with _path(wire, "http", "/r301/log.txt").open("ab") as stream:
            stream.write(b"two\n")
    assert wire.files == {"/store/log.txt": b"one\n"}
    assert [r.method for r in wire.log] == ["GET", "GET", "PUT"]


@pytest.mark.parametrize("status", _RESENT)
def test_patch_append_through_307_or_308_sends_range_and_bytes_to_the_target(
    wire, status
):
    wire.files["/store/log.txt"] = b"one\n"
    path = _path(wire, "http", f"/r{status}/log.txt", append_mode="patch")
    with path.open("ab") as stream:
        stream.write(b"two\n")
    patches = wire.sent("PATCH")
    assert [(r.path, r.headers["Content-Range"], r.body) for r in patches] == [
        (f"/r{status}/log.txt", "bytes 4-7/*", b"two\n"),
        ("/store/log.txt", "bytes 4-7/*", b"two\n"),
    ]
    assert wire.files["/store/log.txt"] == b"one\ntwo\n"


def test_patch_append_through_a_302_does_not_change_the_file(wire):
    wire.files["/store/log.txt"] = b"one\n"
    path = _path(wire, "http", "/r302/log.txt", append_mode="patch")
    with pytest.raises(OSError):
        with path.open("ab") as stream:
            stream.write(b"two\n")
    assert [r.method for r in wire.sent("PATCH", "PUT", "GET")] == ["PATCH"]
    assert wire.files == {"/store/log.txt": b"one\n"}


def test_post_write_method_through_a_302_raises(wire):
    wire.files["/store/f.bin"] = b"ORIGINAL"
    path = _path(wire, "http", "/r302/f.bin", write_method="POST")
    with pytest.raises(OSError) as excinfo:
        path.write_bytes(b"NEW")
    _refused(excinfo, 302, f"{wire.base}/store/f.bin")
    assert [(r.method, r.path) for r in wire.log] == [("POST", "/r302/f.bin")]


def test_a_second_redirect_raises(wire):
    wire.on("PUT", "/chain/f.bin", (307, {"Location": "/r307/f.bin"}, b""))
    with pytest.raises(OSError) as excinfo:
        _path(wire, "http", "/chain/f.bin").write_bytes(b"NEW")
    _refused(excinfo, 307, f"{wire.base}/store/f.bin")
    assert [(r.method, r.path) for r in wire.log] == [
        ("PUT", "/chain/f.bin"),
        ("PUT", "/r307/f.bin"),
    ]
    assert wire.files == {}


@pytest.mark.parametrize("scheme", _SCHEMES)
@pytest.mark.parametrize(
    "target",
    [
        "http://localhost:{port}/store/f.bin",
        "http://127.0.0.1:1/store/f.bin",
        "https://127.0.0.1:{port}/store/f.bin",
        "//localhost:{port}/store/f.bin",
    ],
    ids=["other-host", "other-port", "other-scheme", "other-host-no-scheme"],
)
def test_307_to_another_scheme_host_or_port_is_not_followed(wire, scheme, target):
    target = target.format(port=wire.port)
    wire.on("PUT", "/away/f.bin", (307, {"Location": target}, b""))
    with pytest.raises(OSError) as excinfo:
        _path(wire, scheme, "/away/f.bin").write_bytes(b"NEW")
    expected = "http:" + target if target.startswith("//") else target
    _refused(excinfo, 307, expected)
    assert [r.path for r in wire.log] == ["/away/f.bin"]


def test_a_307_without_a_location_raises(wire):
    wire.on("PUT", "/nowhere", (307, {}, b""))
    with pytest.raises(OSError) as excinfo:
        _path(wire, "http", "/nowhere").write_bytes(b"NEW")
    assert excinfo.value.errno == errno.EIO and "307" in str(excinfo.value)
    assert len(wire.log) == 1


def test_a_redirect_location_never_shows_its_userinfo(wire):
    target = f"http://user:hunter2@127.0.0.1:{wire.port}/store/f.bin"
    wire.on("PUT", "/leak", (301, {"Location": target}, b""))
    with pytest.raises(OSError) as excinfo:
        _path(wire, "http", "/leak").write_bytes(b"NEW")
    _refused(excinfo, 301, f"{wire.base}/store/f.bin")
    assert "hunter2" not in str(excinfo.value) and "user:" not in str(excinfo.value)


def test_a_followed_redirect_ignores_userinfo_in_its_location(wire):
    target = f"http://user:hunter2@127.0.0.1:{wire.port}/store/f.bin"
    wire.on("PUT", "/leak", (307, {"Location": target}, b""))
    _path(wire, "http", "/leak").write_bytes(b"NEW")
    assert [r.path for r in wire.log] == ["/leak", "/store/f.bin"]
    assert all("Authorization" not in r.headers for r in wire.log)
    assert wire.files == {"/store/f.bin": b"NEW"}


def test_a_followed_redirect_keeps_the_request_credentials(wire):
    url = f"http://user:hunter2@127.0.0.1:{wire.port}/r307/f.bin"
    UriPath(url).with_session(requests.Session(), timeout=TIMEOUT).write_bytes(b"NEW")
    first, second = wire.log
    assert first.headers["Authorization"] == second.headers["Authorization"]
    assert first.headers["Authorization"].startswith("Basic ")


def test_a_refused_redirect_names_the_url_without_credentials(wire):
    url = f"http://user:hunter2@127.0.0.1:{wire.port}/r302/f.bin"
    path = UriPath(url).with_session(requests.Session(), timeout=TIMEOUT)
    with pytest.raises(OSError) as excinfo:
        path.write_bytes(b"NEW")
    assert "hunter2" not in str(excinfo.value)
    assert f"{wire.base}/r302/f.bin" in str(excinfo.value)


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_allow_redirects_in_the_session_arguments_does_not_follow_a_write(wire, scheme):
    wire.files["/store/f.bin"] = b"ORIGINAL"
    path = _path(wire, scheme, "/r302/f.bin", allow_redirects=True)
    with pytest.raises(OSError):
        path.write_bytes(b"NEW")
    assert [r.method for r in wire.log] == ["PUT"]
    with pytest.raises(OSError):
        path.backend.request(
            "PUT", f"{wire.base}/r302/f.bin", data=b"NEW", allow_redirects=True
        )
    assert [r.method for r in wire.log] == ["PUT", "PUT"]
    assert wire.files == {"/store/f.bin": b"ORIGINAL"}


def test_a_body_that_cannot_be_sent_twice_is_not_resent_after_a_307(wire):
    path = _path(wire, "http", "/r307/f.bin")
    with pytest.raises(OSError) as excinfo:
        path.backend.request("PUT", f"{wire.base}/r307/f.bin", data=io.BytesIO(b"abc"))
    assert excinfo.value.errno == errno.EIO
    assert [r.path for r in wire.log] == ["/r307/f.bin"]
    assert wire.files == {}


def test_reads_still_follow_redirects(wire):
    wire.files["/store/f.bin"] = b"CONTENT"
    assert _path(wire, "http", "/r302/f.bin").read_bytes() == b"CONTENT"
    assert _path(wire, "dav", "/r307/f.bin").stat().st_size == 7
    assert [r.method for r in wire.log] == ["GET", "GET", "PROPFIND", "PROPFIND"]


# --- exclusive create ("x") and the size patch-mode append starts from ---

_PROBE_FAILURES = {
    "500": ((500, {}, b""), OSError),
    "403": ((403, {}, b""), PermissionError),
    "429": ((429, {}, b""), OSError),
    "503": ((503, {}, b""), OSError),
    "timeout": (_stall, TimeoutError),
}
_PROBE = {"http": "HEAD", "dav": "PROPFIND"}


@pytest.mark.parametrize("scheme", _SCHEMES)
@pytest.mark.parametrize("failure", _PROBE_FAILURES)
def test_exclusive_create_whose_existence_probe_fails_raises_and_sends_no_put(
    wire, scheme, failure
):
    reply, error = _PROBE_FAILURES[failure]
    wire.files["/f.bin"] = b"PRECIOUS"
    wire.on(_PROBE[scheme], "/f.bin", reply)
    with pytest.raises(error):
        _path(wire, scheme, "/f.bin").open("xb")
    assert [r.method for r in wire.log] == [_PROBE[scheme]]
    assert wire.files == {"/f.bin": b"PRECIOUS"}


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_exclusive_create_of_an_existing_file_raises_and_sends_no_put(wire, scheme):
    wire.files["/f.bin"] = b"PRECIOUS"
    with pytest.raises(FileExistsError) as excinfo:
        _path(wire, scheme, "/f.bin").open("xb")
    assert excinfo.value.errno == errno.EEXIST
    assert [r.method for r in wire.log] == [_PROBE[scheme]]
    assert wire.files == {"/f.bin": b"PRECIOUS"}


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_exclusive_create_sends_if_none_match_and_the_session_headers(wire, scheme):
    path = _path(wire, scheme, "/new.bin", headers={"X-Token": "t"})
    with path.open("xb") as stream:
        stream.write(b"CREATED")
    (put,) = wire.sent("PUT")
    assert put.headers["If-None-Match"] == "*"
    assert put.headers["X-Token"] == "t"
    assert wire.files == {"/new.bin": b"CREATED"}


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_exclusive_create_is_refused_by_the_server_when_the_file_appeared(wire, scheme):
    # The probe is told the file is missing, as if it was created since.
    wire.files["/f.bin"] = b"PRECIOUS"
    wire.on(_PROBE[scheme], "/f.bin", (404, {}, b""))
    with pytest.raises(FileExistsError) as excinfo:
        with _path(wire, scheme, "/f.bin").open("xb") as stream:
            stream.write(b"CLOBBER")
    assert excinfo.value.errno == errno.EEXIST
    assert wire.files == {"/f.bin": b"PRECIOUS"}


@pytest.mark.parametrize("scheme", _SCHEMES)
def test_plain_write_sends_no_if_none_match(wire, scheme):
    _path(wire, scheme, "/w.bin").write_bytes(b"data")
    assert all("If-None-Match" not in r.headers for r in wire.log)
    assert wire.files == {"/w.bin": b"data"}


def test_rewrite_append_sends_no_if_none_match(wire):
    wire.files["/log.txt"] = b"one\n"
    with _path(wire, "http", "/log.txt").open("ab") as stream:
        stream.write(b"two\n")
    assert all("If-None-Match" not in r.headers for r in wire.log)
    assert wire.files == {"/log.txt": b"one\ntwo\n"}


_NO_LENGTH = (200, {"Transfer-Encoding": "chunked"}, b"")


def test_patch_append_refuses_a_head_reply_without_content_length(wire):
    wire.files["/log.txt"] = b"0123456789"
    wire.on("HEAD", "/log.txt", _NO_LENGTH)
    path = _path(wire, "http", "/log.txt", append_mode="patch")
    with pytest.raises(OSError) as excinfo:
        path.open("ab")
    assert excinfo.value.errno == errno.EIO
    assert str(path) in str(excinfo.value) and "Content-Length" in str(excinfo.value)
    gc.collect()
    assert [r.method for r in wire.log] == ["HEAD"]
    assert wire.files == {"/log.txt": b"0123456789"}


def test_patch_append_to_an_empty_file_starts_at_offset_zero(wire):
    wire.files["/empty.txt"] = b""
    path = _path(wire, "http", "/empty.txt", append_mode="patch")
    with path.open("ab") as stream:
        stream.write(b"abc")
    (patch,) = wire.sent("PATCH")
    assert patch.headers["Content-Range"] == "bytes 0-2/*"
    assert wire.files == {"/empty.txt": b"abc"}


def test_stat_still_reports_size_zero_for_a_head_reply_without_content_length(wire):
    wire.files["/log.txt"] = b"0123456789"
    wire.on("HEAD", "/log.txt", _NO_LENGTH)
    assert _path(wire, "http", "/log.txt").stat().st_size == 0
