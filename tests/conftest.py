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
    """In-process SFTP server backed by fixture_tree, using paramiko's
    ServerInterface + SFTPServerInterface.  No extra dependencies -- paramiko
    is already required by the `sftp` extra."""
    paramiko = pytest.importorskip("paramiko")
    import errno
    import os
    import socket
    import stat as _stat
    import threading

    root = str(fixture_tree)

    # -- OS-backed SFTP implementation -----------------------------------------
    class _SFTPInterface(paramiko.SFTPServerInterface):
        def _realpath(self, path):
            # Strip leading '/' and join under root; clamp to root (no escapes)
            return os.path.join(root, path.lstrip("/"))

        def list_folder(self, path):
            real = self._realpath(path)
            try:
                names = os.listdir(real)
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            out = []
            for name in names:
                attr = paramiko.SFTPAttributes.from_stat(
                    os.stat(os.path.join(real, name))
                )
                attr.filename = name
                out.append(attr)
            return out

        def stat(self, path):
            real = self._realpath(path)
            try:
                return paramiko.SFTPAttributes.from_stat(os.stat(real))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def lstat(self, path):
            real = self._realpath(path)
            try:
                return paramiko.SFTPAttributes.from_stat(os.lstat(real))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)

        def open(self, path, flags, attr):
            real = self._realpath(path)
            try:
                binary_flag = getattr(os, "O_BINARY", 0)
                fd = os.open(real, flags | binary_flag, 0o666)
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            fobj = os.fdopen(fd, "r+b" if (flags & os.O_RDWR) else
                             ("wb" if (flags & os.O_WRONLY) else "rb"))
            handle = paramiko.SFTPHandle(flags)
            handle.filename = real
            handle.readfile = fobj
            handle.writefile = fobj
            return handle

        def remove(self, path):
            try:
                os.remove(self._realpath(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def rename(self, oldpath, newpath):
            try:
                os.rename(self._realpath(oldpath), self._realpath(newpath))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def mkdir(self, path, attr):
            try:
                os.mkdir(self._realpath(path), 0o755)
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def rmdir(self, path):
            try:
                os.rmdir(self._realpath(path))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def chattr(self, path, attr):
            real = self._realpath(path)
            try:
                if attr.st_mode is not None:
                    os.chmod(real, _stat.S_IMODE(attr.st_mode))
            except OSError as e:
                return paramiko.SFTPServer.convert_errno(e.errno)
            return paramiko.SFTP_OK

        def canonicalize(self, path):
            real = self._realpath(path)
            return "/" + os.path.relpath(real, root).replace("\\", "/")

    # -- Minimal SSH server (no auth) ------------------------------------------
    class _SSHServer(paramiko.ServerInterface):
        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED

        def check_auth_none(self, username):
            return paramiko.AUTH_SUCCESSFUL

        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL

        def get_allowed_auths(self, username):
            return "none,password"

        # No override of check_channel_subsystem_request(): the base
        # paramiko.ServerInterface implementation is what actually looks up
        # the handler registered via set_subsystem_handler() and starts it
        # (handler.start()) -- an override that just returns `name == "sftp"`
        # (as this used to) skips that entirely, so the channel is reported
        # "hooked up" but nothing server-side ever reads/responds, and the
        # client's SFTPClient.from_transport() hangs forever waiting for the
        # version-negotiation reply that nothing sends.

    # -- TCP accept loop -------------------------------------------------------
    host_key = paramiko.RSAKey.generate(2048)
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
    listen_sock.bind(("127.0.0.1", 0))
    listen_sock.listen(20)
    listen_sock.settimeout(0.5)
    port = listen_sock.getsockname()[1]
    stop_event = threading.Event()

    def _serve():
        while not stop_event.is_set():
            try:
                conn, _ = listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = paramiko.Transport(conn)
            t.add_server_key(host_key)
            t.set_subsystem_handler("sftp", paramiko.SFTPServer, _SFTPInterface)
            t.start_server(server=_SSHServer())

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield f"sftp://user:test@127.0.0.1:{port}/"
    finally:
        stop_event.set()
        listen_sock.close()
        thread.join(timeout=5)
