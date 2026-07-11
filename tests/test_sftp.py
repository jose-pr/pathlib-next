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


def test_sftpclient_cached_across_accesses():
    backend = _FakeBackend()
    p = _sftp("sftp://host/a", backend=backend)
    client1 = p._sftpclient
    client2 = p._sftpclient
    assert client1 is client2
    assert backend.client_calls == 1


def test_sftpclient_recreated_when_socket_inactive():
    backend = _FakeBackend()
    p = _sftp("sftp://host/a", backend=backend)
    client1 = p._sftpclient
    client1.sock.active = False
    backend._client = _FakeSftpClient()  # what a fresh connect would return
    client2 = p._sftpclient
    assert client2 is not client1
    assert backend.client_calls == 2


def test_sftpclient_different_sources_not_shared():
    backend = _FakeBackend()
    p1 = _sftp("sftp://host1/a", backend=backend)
    p2 = _sftp("sftp://host2/a", backend=backend)
    p1._sftpclient
    p2._sftpclient
    assert backend.client_calls == 2


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
