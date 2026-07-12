import functools
import http.server
import threading
import time

import pytest

from pathlib_next.mempath import MemPath, MemPathBackend


@pytest.fixture
def fixture_tree(tmp_path):
    """A small fixture tree for glob/walk/contract tests:

    root/
      a.txt
      b.py
      .hidden.txt
      sub/
        c.py
        nested/
          d.py
      empty_dir/
    """
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.py").write_text("b")
    (tmp_path / ".hidden.txt").write_text("hidden")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("c")
    nested = sub / "nested"
    nested.mkdir()
    (nested / "d.py").write_text("d")
    (tmp_path / "empty_dir").mkdir()
    return tmp_path


@pytest.fixture
def mem_backend():
    return MemPathBackend()


@pytest.fixture
def mem_root(mem_backend):
    return MemPath("/", backend=mem_backend)


@pytest.fixture
def http_server(fixture_tree):
    """Serve fixture_tree over HTTP via stdlib's ThreadingHTTPServer with
    directory-listing support, on an OS-assigned port. Yields the base URL
    (no trailing slash)."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(fixture_tree)
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _CannedListingHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with `list_directory()` overridden to emit
    Apache-`<pre>`- or nginx-`<table>`-style listing HTML instead of the
    stdlib's bare `<ul><li>` -- the real `http_server` fixture never
    exercises `_DirectoryListingParser`'s two primary (non-fallback) code
    paths at all, since SimpleHTTPRequestHandler only ever emits `<ul>`.
    File GET/HEAD/redirect behavior is inherited unchanged from the stdlib
    handler; only the directory-index HTML differs. `listing_format` is a
    class attr set per-fixture via subclassing (not `functools.partial`,
    since we need to override a method, not just bind __init__ args)."""

    listing_format = "pre"  # or "table"

    def list_directory(self, path):
        import io
        import os
        import urllib.parse

        try:
            entries = os.listdir(path)
        except OSError:
            self.send_error(404, "No permission to list directory")
            return None
        entries.sort(key=lambda a: a.lower())

        rows = []
        for name in entries:
            fullname = os.path.join(path, name)
            is_dir = os.path.isdir(fullname)
            displayname = name + ("/" if is_dir else "")
            href = urllib.parse.quote(displayname)
            st = os.stat(fullname)
            mtime = time.strftime("%d-%b-%Y %H:%M:%S", time.localtime(st.st_mtime))
            size = "-" if is_dir else str(st.st_size)
            if self.listing_format == "pre":
                pad = " " * max(1, 45 - len(displayname))
                rows.append(f'<a href="{href}">{displayname}</a>{pad}{mtime}  {size}\n')
            else:
                rows.append(
                    f'<tr><td><a href="{href}">{displayname}</a></td>'
                    f"<td>{mtime}</td><td align=\"right\">{size}</td>"
                    f"<td>&nbsp;</td></tr>\n"
                )
        body = "".join(rows)

        title = f"Index of {self.path}"
        if self.listing_format == "pre":
            html = (
                f"<html><head><title>{title}</title></head><body>"
                f'<h1>{title}</h1><pre><a href="../">../</a>\n{body}</pre>'
                "</body></html>"
            )
        else:
            head = (
                '<tr><th><a href="?C=N;O=D">Name</a></th>'
                '<th><a href="?C=M;O=A">Last modified</a></th>'
                '<th><a href="?C=S;O=A">Size</a></th><th>Description</th></tr>\n'
                '<tr><th colspan="4"><hr></th></tr>\n'
            )
            parent = (
                '<tr><td><a href="../">Parent Directory</a></td>'
                '<td>&nbsp;</td><td align="right">-</td><td>&nbsp;</td></tr>\n'
            )
            html = (
                f"<html><head><title>{title}</title></head><body>"
                f"<h1>{title}</h1><table>\n{head}{parent}{body}"
                "</table></body></html>"
            )

        encoded = html.encode("utf-8", "surrogateescape")
        f = io.BytesIO(encoded)
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        return f

    def log_message(self, format, *args):
        pass


class _ApachePreListingHandler(_CannedListingHandler):
    listing_format = "pre"


class _NginxTableListingHandler(_CannedListingHandler):
    listing_format = "table"


def _start_canned_server(handler_cls, fixture_tree):
    handler = functools.partial(handler_cls, directory=str(fixture_tree))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def http_server_apache_pre(fixture_tree):
    """Like `http_server`, but directory listings are Apache
    `mod_autoindex`-style `<pre>` HTML -- drives
    `_DirectoryListingParser`'s `<pre>` code path end-to-end."""
    yield from _start_canned_server(_ApachePreListingHandler, fixture_tree)


@pytest.fixture
def http_server_nginx_table(fixture_tree):
    """Like `http_server`, but directory listings are nginx
    `autoindex`-style `<table>` HTML -- drives
    `_DirectoryListingParser`'s `<table>` code path end-to-end."""
    yield from _start_canned_server(_NginxTableListingHandler, fixture_tree)


@pytest.fixture
def http_status_server():
    """Serves canned status codes / delays on demand, driving
    `_translate_http_errors` from real `requests` exceptions instead of
    hand-constructed ones: GET/HEAD/PUT/DELETE `/<code>` returns that
    status; `/timeout` sleeps past any client-side timeout. Yields the
    base URL (no trailing slash)."""

    class _StatusHandler(http.server.BaseHTTPRequestHandler):
        def _handle(self):
            path = self.path.lstrip("/")
            if path == "timeout":
                time.sleep(5)
                return
            try:
                code = int(path)
            except ValueError:
                code = 404
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_HEAD = do_PUT = do_DELETE = _handle

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _WritableHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler + real PUT/DELETE against files on disk --
    for a genuine write-then-read-back round trip. Existing
    `test_http_write_*`/`test_http_unlink` tests only monkeypatch
    `requests.Session.request` and assert the request was sent; they never
    verify data actually persists server-side."""

    def do_PUT(self):
        import pathlib

        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        p = pathlib.Path(self.translate_path(self.path))
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.send_response(204 if existed else 201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        import pathlib

        p = pathlib.Path(self.translate_path(self.path))
        if not p.exists():
            self.send_error(404)
            return
        p.unlink()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


class _HeadRejectingHandler(http.server.SimpleHTTPRequestHandler):
    """GET works normally; HEAD always 405s -- drives `stat()`'s
    HEAD-405-fallback-to-GET path against a real server."""

    def do_HEAD(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


@pytest.fixture
def http_writable_server(fixture_tree):
    yield from _start_canned_server(_WritableHandler, fixture_tree)


@pytest.fixture
def http_server_head_rejecting(fixture_tree):
    yield from _start_canned_server(_HeadRejectingHandler, fixture_tree)


@pytest.fixture
def unused_tcp_port():
    """A port nobody is listening on, for connection-refused coverage."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def ftp_server(fixture_tree):
    import threading
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    authorizer = DummyAuthorizer()
    authorizer.add_user("user", "12345", str(fixture_tree), perm="elradfmwMT")
    handler = FTPHandler
    handler.authorizer = authorizer
    server = FTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.socket.getsockname()[1]
        yield f"ftp://user:12345@127.0.0.1:{port}/"
    finally:
        server.close_all()
        thread.join(timeout=5)


@pytest.fixture
def dav_server(fixture_tree):
    import time
    import threading
    import wsgidav.wsgidav_app
    from cheroot import wsgi

    config = {
        "host": "127.0.0.1",
        "port": 0,
        "provider_mapping": {"/": str(fixture_tree)},
        "simple_dc": {"user_mapping": {"*": True}},
        "logging": {"enable": False}
    }
    app = wsgidav.wsgidav_app.WsgiDAVApp(config)
    server = wsgi.Server(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    while not server.ready:
        time.sleep(0.01)
    try:
        yield f"dav://127.0.0.1:{server.bind_addr[1]}"
    finally:
        server.stop()
        thread.join(timeout=5)


@pytest.fixture
def s3_server(fixture_tree):
    """In-process moto S3 mock with a pre-populated 'test-bucket' matching
    the standard fixture_tree layout required by ReadPathContract/PathContract."""
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws
    import os
    import pathlib

    def _upload_tree(client, bucket, local_dir, prefix=""):
        local_dir = pathlib.Path(local_dir)
        for entry in local_dir.iterdir():
            rel = (prefix + "/" + entry.name).lstrip("/")
            if entry.is_dir():
                # upload a marker so the directory appears in listings
                client.put_object(Bucket=bucket, Key=rel + "/", Body=b"")
                _upload_tree(client, bucket, entry, rel)
            else:
                client.put_object(Bucket=bucket, Key=rel, Body=entry.read_bytes())

    with mock_aws():
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        _upload_tree(client, "test-bucket", fixture_tree)
        yield "s3://test-bucket", None


@pytest.fixture
def sftp_server(fixture_tree):
    """In-process SFTP server backed by fixture_tree, using asyncssh's own
    `SFTPServer` (chrooted) -- a full local-filesystem-backed SFTP server
    for free, replacing what used to be a ~150-line hand-rolled paramiko
    `ServerInterface`/`SFTPServerInterface`. A CLIENT backend choice
    (paramiko or asyncssh, see `TestSftpContract`) is independent of which
    library the test SERVER uses -- an asyncssh server talks standard SFTP
    to a paramiko client fine (verified). Runs on its own dedicated event
    loop/thread, independent of `AsyncsshSftpBackend`'s shared bridge loop
    (server and client are logically separate machines in reality; keeping
    them on separate loops here mirrors that instead of coupling test
    infrastructure to backend-internal state)."""
    asyncssh = pytest.importorskip("asyncssh")
    import asyncio
    import sys

    root = str(fixture_tree)

    class _NoAuth(asyncssh.SSHServer):
        def begin_auth(self, username):
            return False

    def _sftp_factory(chan):
        return asyncssh.SFTPServer(chan, chroot=root)

    async def _start():
        return await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=_NoAuth,
            server_host_keys=[asyncssh.generate_private_key("ssh-rsa")],
            sftp_factory=_sftp_factory,
            process_factory=None,
        )

    # SelectorEventLoop on Windows, not the WindowsProactorEventLoopPolicy
    # default -- avoids a benign-but-noisy Proactor pipe-transport __del__
    # warning (see _asyncssh.py's _new_loop() for the same fix applied to
    # the real backend's bridge loop); we don't need subprocess pipes here.
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = asyncio.run_coroutine_threadsafe(_start(), loop).result()
    port = server.sockets[0].getsockname()[1]
    try:
        # A password is included even though the server's begin_auth()
        # accepts everything unconditionally: paramiko's SSHClient.connect()
        # only sends an auth attempt at all if at least one of
        # key/keyfiles/password is available -- with allow_agent=False,
        # look_for_keys=False and no password, it raises "No authentication
        # methods available" client-side before ever reaching the server.
        yield f"sftp://x:x@127.0.0.1:{port}/"
    finally:
        # server.close() must run ON THE LOOP'S OWN THREAD -- asyncio/
        # asyncssh objects aren't thread-safe, and calling close() directly
        # from this (test) thread races the loop thread still processing
        # I/O, corrupting internal state (observed: TypeError from
        # asyncio.Server._wakeup() on `self._waiters` -- not a double
        # -close, a torn read of state being mutated concurrently).
        # Not blocking on wait_closed(): it waits for every accepted
        # connection to close too, and a long-lived cached client
        # (paramiko's connection cache, or an asyncssh backend instance a
        # test didn't explicitly tear down) can leave one open, hanging
        # this indefinitely.
        loop.call_soon_threadsafe(server.close)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
