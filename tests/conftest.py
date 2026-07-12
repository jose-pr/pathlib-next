import functools
import http.server
import threading

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
