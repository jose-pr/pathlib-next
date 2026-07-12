"""Unit-only SFTP tests: mock BaseSftpBackend, no real server. Covers
Source->connect_opts mapping, client cache keying/invalidation, and the
B12/B13 regressions (chmod follow_symlinks, rename target.path).
"""
import pytest

pytest.importorskip("paramiko")

from pathlib_next.uri import Source, Uri
from pathlib_next.uri.schemes.sftp import BaseSftpBackend, SftpBackend, SftpPath


class _FakeSock:
    def __init__(self, active=True):
        self.active = active


class _FakeAttr:
    def __init__(self, filename, st_mode=0):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = 0
        self.st_mtime = 0


class _FakeSftpClient:
    def __init__(self):
        self.sock = _FakeSock(True)
        self.rename_calls = []
        self.chmod_calls = []

    def rename(self, path, target):
        self.rename_calls.append((path, target))

    def chmod(self, path, mode):
        self.chmod_calls.append((path, mode))

    def listdir(self, path):
        return ["a", "b"]

    def listdir_attr(self, path):
        return [_FakeAttr("a"), _FakeAttr("b")]

    def stat(self, path):
        return object()

    def open(self, path, mode, buffering):
        return object()

    def remove(self, path):
        pass

    def rmdir(self, path):
        pass

    def mkdir(self, path, mode):
        pass


class _FakeBackend(BaseSftpBackend):
    def __init__(self):
        self.client_calls = 0
        self._client = _FakeSftpClient()

    def client(self, source):
        self.client_calls += 1
        return self._client


def _sftp(path, backend=None):
    return SftpPath(path, backend=backend or _FakeBackend())


# --- Source -> connect_opts mapping ---


def test_opts_maps_host_port_user_password():
    backend = SftpBackend({}, None)
    source = Source("sftp", "user:pass", "host", 2222)
    opts = backend.opts(source)
    assert opts["hostname"] == "host"
    assert opts["port"] == 2222
    assert opts["username"] == "user"
    assert opts["password"] == "pass"


def test_opts_default_port_22():
    backend = SftpBackend({}, None)
    source = Source("sftp", None, "host", None)
    opts = backend.opts(source)
    assert opts["port"] == 22
    assert "username" not in opts
    assert "password" not in opts


def test_opts_merges_connect_opts():
    backend = SftpBackend({"timeout": 5}, None)
    source = Source("sftp", None, "host", None)
    opts = backend.opts(source)
    assert opts["timeout"] == 5


# --- client cache keying/invalidation ---
# SftpPath._sftpclient is a trivial `self.backend.client(self.source)`
# delegation (post-schemes_layout/asyncssh_sftp split) -- caching is each
# backend's own responsibility, not SftpPath's. _FakeBackend deliberately
# does no caching of its own (see its `client()` above), so these test
# SftpBackend's (paramiko) real cache/invalidation logic directly instead.


class _FakeTransport:
    def __init__(self):
        self.clients = []

    def open_sftp_client(self):
        client = _FakeSftpClient()
        self.clients.append(client)
        return client


def test_sftp_backend_client_cached_across_calls(monkeypatch):
    backend = SftpBackend({}, None)
    transport = _FakeTransport()
    monkeypatch.setattr(SftpBackend, "transport", lambda self, source: transport)
    source = Source("sftp", None, "host", None)
    client1 = backend.client(source)
    client2 = backend.client(source)
    assert client1 is client2
    assert len(transport.clients) == 1


def test_sftp_backend_client_recreated_when_socket_inactive(monkeypatch):
    backend = SftpBackend({}, None)
    transport = _FakeTransport()
    monkeypatch.setattr(SftpBackend, "transport", lambda self, source: transport)
    source = Source("sftp", None, "host", None)
    client1 = backend.client(source)
    client1.sock.active = False
    client2 = backend.client(source)
    assert client2 is not client1
    assert len(transport.clients) == 2


def test_sftp_backend_client_different_sources_not_shared(monkeypatch):
    backend = SftpBackend({}, None)
    transport = _FakeTransport()
    monkeypatch.setattr(SftpBackend, "transport", lambda self, source: transport)
    backend.client(Source("sftp", None, "host1", None))
    backend.client(Source("sftp", None, "host2", None))
    assert len(transport.clients) == 2


# --- B12: chmod follow_symlinks ---


def test_chmod_follow_symlinks_true_delegates():
    backend = _FakeBackend()
    p = _sftp("sftp://host/a.txt", backend=backend)
    p.chmod(0o644)
    assert backend._client.chmod_calls == [("/a.txt", 0o644)]


def test_chmod_follow_symlinks_false_raises_notimplemented():
    backend = _FakeBackend()
    p = _sftp("sftp://host/a.txt", backend=backend)
    with pytest.raises(NotImplementedError):
        p.chmod(0o644, follow_symlinks=False)


# --- B13: rename target.path, not target.as_posix() ---


def test_rename_uses_target_path_not_as_posix():
    backend = _FakeBackend()
    p = _sftp("sftp://host/a.txt", backend=backend)
    target = Uri("sftp://host/b.txt")
    p.rename(target)
    # as_posix() would have been "host:/b.txt" (Uri.as_posix() prefixes
    # host:); the SFTP wire protocol only wants the raw path.
    assert backend._client.rename_calls == [("/a.txt", "/b.txt")]


def test_rename_accepts_str_target():
    backend = _FakeBackend()
    p = _sftp("sftp://host/a.txt", backend=backend)
    p.rename("b.txt")
    assert backend._client.rename_calls == [("/a.txt", "/b.txt")]


def test_sftp_backend_connect_and_client():
    import unittest.mock
    mock_ssh = unittest.mock.MagicMock()
    mock_transport = unittest.mock.MagicMock()
    mock_sftp = unittest.mock.MagicMock()
    
    mock_ssh.get_transport.return_value = mock_transport
    mock_transport.open_sftp_client.return_value = mock_sftp
    
    with unittest.mock.patch("paramiko.SSHClient", return_value=mock_ssh):
        backend = SftpBackend({"timeout": 10}, "policy")
        source = Source("sftp", "user:pass", "host", 2222)
        
        # Test transport()
        transport = backend.transport(source)
        assert transport is mock_transport
        mock_ssh.set_missing_host_key_policy.assert_called_with("policy")
        mock_ssh.connect.assert_called_with(
            timeout=10,
            hostname="host",
            port=2222,
            username="user",
            password="pass"
        )
        
        # Test client()
        client = backend.client(source)
        assert client is mock_sftp
        mock_transport.open_sftp_client.assert_called_once()
        
        # Test transport raising if None
        mock_ssh.get_transport.return_value = None
        with pytest.raises(Exception):
            backend.transport(source)


def test_sftppath_operations():
    class _OperationsFakeSftpClient(_FakeSftpClient):
        def __init__(self):
            super().__init__()
            self.actions = []

        def listdir(self, path):
            self.actions.append(("listdir", path))
            return ["file1", "file2"]

        def listdir_attr(self, path):
            self.actions.append(("listdir_attr", path))
            return [_FakeAttr("file1"), _FakeAttr("file2")]

        def stat(self, path):
            self.actions.append(("stat", path))
            from pathlib_next.utils.stat import FileStat
            return FileStat(is_dir=True)

        def lstat(self, path):
            self.actions.append(("lstat", path))
            from pathlib_next.utils.stat import FileStat
            return FileStat(is_dir=False)

        def open(self, path, mode, buffering):
            self.actions.append(("open", path, mode, buffering))
            import io
            return io.BytesIO(b"data")

        def mkdir(self, path, mode):
            self.actions.append(("mkdir", path, mode))

        def remove(self, path):
            self.actions.append(("remove", path))

        def rmdir(self, path):
            self.actions.append(("rmdir", path))

    class _OperationsFakeBackend(BaseSftpBackend):
        def __init__(self):
            self._client = _OperationsFakeSftpClient()
        def client(self, source):
            return self._client

    backend = _OperationsFakeBackend()
    p = _sftp("sftp://host/dir", backend=backend)

    # listdir_attr via iterdir (scandir contract: one call for the whole
    # listing, metadata included -- no per-child stat())
    children = list(p.iterdir())
    assert [c.name for c in children] == ["file1", "file2"]
    assert backend._client.actions[-1] == ("listdir_attr", "/dir")

    # stat
    p.stat(follow_symlinks=True)
    assert backend._client.actions[-1] == ("stat", "/dir")
    p.stat(follow_symlinks=False)
    assert backend._client.actions[-1] == ("lstat", "/dir")

    # open
    p.open("r", 1024)
    assert backend._client.actions[-1] == ("open", "/dir", "r", 1024)

    # mkdir
    p.mkdir(0o755)
    assert any(a[0] == "mkdir" for a in backend._client.actions)

    # unlink
    p.unlink(missing_ok=True)
    assert backend._client.actions[-1] == ("remove", "/dir")

    # rmdir
    p.rmdir()
    assert backend._client.actions[-1] == ("rmdir", "/dir")

