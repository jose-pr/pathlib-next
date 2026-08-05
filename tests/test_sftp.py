"""Unit-only SFTP tests: mock BaseSftpBackend, no real server. Covers
Source->connect_opts mapping, client cache keying/invalidation, and the
B12/B13 regressions (chmod follow_symlinks, rename target.path).
"""

import os

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

    def lstat(self, path):
        return self.stat(path)

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


def test_opts_uses_ssh_config_defaults(monkeypatch):
    backend = SftpBackend({}, None)
    monkeypatch.setattr(
        "pathlib_next.uri.schemes.sftp._paramiko._lookup_ssh_config",
        lambda host, ssh_config: {
            "hostname": "real-host",
            "port": "2200",
            "user": "cfg-user",
            "identityfile": ["id_test"],
            "proxycommand": "ssh jump nc %h %p",
        },
    )
    opts = backend.opts(Source("sftp", None, "alias-host", None))
    assert opts["hostname"] == "real-host"
    assert opts["port"] == 2200
    assert opts["username"] == "cfg-user"
    assert opts["key_filename"] == ["id_test"]
    assert "sock" in opts


def test_source_credentials_override_ssh_config(monkeypatch):
    backend = SftpBackend({}, None)
    monkeypatch.setattr(
        "pathlib_next.uri.schemes.sftp._paramiko._lookup_ssh_config",
        lambda host, ssh_config: {"port": "2200", "user": "cfg-user"},
    )
    opts = backend.opts(Source("sftp", "url-user:url-pass", "host", 2222))
    assert opts["port"] == 2222
    assert opts["username"] == "url-user"
    assert opts["password"] == "url-pass"


def test_sftppath_default_backend_uses_system_ssh_config(monkeypatch):
    recorded = {}

    def _fake_default(cls, ssh_config):
        recorded["ssh_config"] = ssh_config
        return object()

    monkeypatch.setattr(SftpBackend, "default", classmethod(_fake_default))

    class _PinnedSftpPath(SftpPath):
        _default_backend_cls = SftpBackend
        __SCHEMES = ()

    inst = _PinnedSftpPath.__new__(_PinnedSftpPath)
    inst._init(Source("sftp", None, "host", None), "/", "", "")
    _ = inst.backend
    from pathlib_next.uri.schemes.sftp._paramiko import _DEFAULT_SSH_CONFIG

    assert recorded["ssh_config"] is _DEFAULT_SSH_CONFIG


def test_sftppath_explicit_ssh_config_disables_system_lookup(monkeypatch):
    recorded = {}

    def _fake_default(cls, ssh_config):
        recorded["ssh_config"] = ssh_config
        return object()

    monkeypatch.setattr(SftpBackend, "default", classmethod(_fake_default))

    class _PinnedSftpPath(SftpPath):
        _default_backend_cls = SftpBackend
        __SCHEMES = ()

    inst = _PinnedSftpPath.__new__(_PinnedSftpPath)
    inst._init(Source("sftp", None, "host", None), "/", "", "", ssh_config=None)
    _ = inst.backend
    assert recorded["ssh_config"] is None


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


def test_fspath_returns_host_path_for_sftp():
    # sftp: is a _host_filesystem_path scheme: __fspath__ returns the
    # path on the URI's OWN host, for building a command line that runs
    # there -- not a locally-openable path.
    p = _sftp("sftp://user:secret@host/etc/x.conf")
    assert os.fspath(p) == "/etc/x.conf"


def test_host_fspath_returns_path_for_sftp():
    p = _sftp("sftp://user:secret@host/etc/x.conf")
    assert p.host_fspath() == "/etc/x.conf"


def test_str_drops_password_but_host_fspath_and_path_do_not():
    p = _sftp("sftp://user:secret@host/etc/x.conf")
    assert "secret" not in str(p)
    assert p.host_fspath() == "/etc/x.conf"
    assert p.path == "/etc/x.conf"
    # full-fidelity round trip (with credentials) is as_uri(sanitize=False)
    assert p.as_uri(sanitize=False) == "sftp://user:secret@host/etc/x.conf"


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
            timeout=10, hostname="host", port=2222, username="user", password="pass"
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


def test_sftppath_recursive_rm_uses_scandir_metadata_bottom_up():
    import stat

    class _TreeFakeSftpClient(_FakeSftpClient):
        def __init__(self):
            super().__init__()
            self.actions = []
            self.tree = {
                "/root": [("sub", True), ("a.txt", False)],
                "/root/sub": [("b.txt", False)],
            }

        def lstat(self, path):
            self.actions.append(("lstat", path))
            return _FakeAttr(path.rsplit("/", 1)[-1], stat.S_IFDIR | 0o755)

        def listdir_attr(self, path):
            self.actions.append(("listdir_attr", path))
            return [
                _FakeAttr(name, (stat.S_IFDIR if is_dir else stat.S_IFREG) | 0o755)
                for name, is_dir in self.tree[path]
            ]

        def remove(self, path):
            self.actions.append(("remove", path))

        def rmdir(self, path):
            self.actions.append(("rmdir", path))

        def stat(self, path):
            raise AssertionError("recursive rm should use lstat/listdir_attr metadata")

    class _TreeFakeBackend(BaseSftpBackend):
        def __init__(self):
            self._client = _TreeFakeSftpClient()

        def client(self, source):
            return self._client

    backend = _TreeFakeBackend()
    _sftp("sftp://host/root", backend=backend).rm(recursive=True)

    assert backend._client.actions == [
        ("lstat", "/root"),
        ("listdir_attr", "/root"),
        ("listdir_attr", "/root/sub"),
        ("remove", "/root/sub/b.txt"),
        ("rmdir", "/root/sub"),
        ("remove", "/root/a.txt"),
        ("rmdir", "/root"),
    ]


# --- native checksum protocol (protocols/checksum.py::NativeChecksum) ------
# `_FakeBackend` (above) never overrides `checksum()`, so it inherits
# `BaseSftpBackend.checksum()`'s `notimplemented` stub -- exercising it
# proves the "server/backend doesn't support this" fallback path for free,
# with no extra fixture. A second fake backend below DOES implement it, to
# prove `SftpPath.checksum()` delegates and returns the digest as-is.


def test_sftppath_checksum_raises_notimplemented_when_backend_lacks_support():
    # Covers both the "no client-library support at all" case (this is
    # exactly what AsyncsshSftpBackend looks like: no checksum() override)
    # and, transitively, PathSyncer's fallback-to-streaming trigger
    # (utils.checksum.native() catches exactly this).
    p = _sftp("sftp://host/a.txt")
    with pytest.raises(NotImplementedError):
        p.checksum()


def test_sftppath_checksum_raises_notimplemented_for_asyncssh_shaped_backend():
    # AsyncsshSftpBackend genuinely has no checksum() override (see
    # sftp/__init__.py's BaseSftpBackend.checksum docstring) -- a bare
    # BaseSftpBackend subclass with only `client()` implemented models that
    # shape without requiring the asyncssh extra to be installed.
    class _AsyncsshShapedBackend(BaseSftpBackend):
        def client(self, source):
            return _FakeSftpClient()

    p = _sftp("sftp://host/a.txt", backend=_AsyncsshShapedBackend())
    with pytest.raises(NotImplementedError):
        p.checksum()


class _ChecksumCapableBackend(BaseSftpBackend):
    """Fake backend that DOES implement native checksums -- proves
    `SftpPath.checksum()` delegates to `backend.checksum()` and returns its
    value unchanged, and that any non-`NotImplementedError` failure from
    the backend is translated to `NotImplementedError` (SftpPath's
    contract: any reason a genuine digest can't be produced must look the
    same to a caller like `PathSyncer`)."""

    def __init__(self, digests=None, error=None):
        self._client = _RegularFileSftpClient()
        self.digests = digests or {}
        self.error = error
        self.calls = []

    def client(self, source):
        return self._client

    def checksum(self, path, algorithm):
        self.calls.append((path.path, algorithm))
        if self.error is not None:
            raise self.error
        return self.digests[algorithm]

    def supported_checksums(self, path):
        return frozenset(self.digests)


class _RegularFileSftpClient(_FakeSftpClient):
    """`_FakeSftpClient` whose default `stat()` (`object()`) and
    `listdir_attr()` (always `["a", "b"]` regardless of path) make any path
    look like a non-empty directory -- fine for the chmod/rename-focused
    tests above, but wrong for checksum tests, where `PathSyncer` needs to
    see a genuine regular file (`is_file() == True`) or it recurses forever
    trying to walk a "directory" that always reports the same two fake
    children. `st_mode=S_IFREG` here matches what a real SFTP `stat()` on
    an actual file returns.
    """

    def stat(self, path):
        import stat as stat_module

        from pathlib_next.utils.stat import FileStat

        return FileStat(st_mode=stat_module.S_IFREG | 0o644, st_size=len(b"x"))

    def lstat(self, path):
        return self.stat(path)


def test_sftppath_checksum_delegates_to_backend():
    backend = _ChecksumCapableBackend(digests={"md5": "deadbeef"})
    p = _sftp("sftp://host/a.txt", backend=backend)
    assert p.checksum() == "deadbeef"
    assert p.checksum("md5") == "deadbeef"
    assert backend.calls == [("/a.txt", "md5"), ("/a.txt", "md5")]


def test_sftppath_checksum_wraps_non_notimplemented_backend_errors():
    # A backend raising something other than NotImplementedError (a
    # transport error, a KeyError for an unadvertised algorithm, ...) must
    # still surface as NotImplementedError to the caller -- SftpPath's
    # whole contract is "raise if a genuine digest can't be produced",
    # regardless of why.
    backend = _ChecksumCapableBackend(error=OSError("connection reset"))
    p = _sftp("sftp://host/a.txt", backend=backend)
    with pytest.raises(NotImplementedError):
        p.checksum()


def test_sftppath_checksum_preserves_explicit_notimplementederror():
    backend = _ChecksumCapableBackend(error=NotImplementedError("no md5 here"))
    p = _sftp("sftp://host/a.txt", backend=backend)
    with pytest.raises(NotImplementedError, match="no md5 here"):
        p.checksum()


def test_sftppath_supported_checksums_empty_when_backend_lacks_support():
    # _FakeBackend (module-level fixture): no checksum()/supported_checksums()
    # override -- inherits BaseSftpBackend's empty-frozenset default. Models
    # AsyncsshSftpBackend's real shape (no client-library support at all).
    p = _sftp("sftp://host/a.txt")
    assert p.supported_checksums() == frozenset()


def test_sftppath_supported_checksums_reflects_backend_advertisement():
    backend = _ChecksumCapableBackend(digests={"md5": "deadbeef"})
    p = _sftp("sftp://host/a.txt", backend=backend)
    assert p.supported_checksums() == frozenset({"md5"})


# --- native checksum: paramiko SftpBackend wire-level implementation ------
# SftpBackend.checksum() speaks the OpenSSH check-file@openssh.com
# extension directly via paramiko's low-level _request()/CMD_EXTENDED --
# these tests fake that primitive to prove the request is built correctly
# (handle, algorithm, int64 offset/length, quick_check) and the reply is
# parsed/validated correctly, without needing a real OpenSSH server. The
# real SFTP test server used elsewhere in this suite is asyncssh's own
# SFTPServer (tests/conftest.py::sftp_server), which has NO
# check-file@openssh.com support at all -- so a real-server round trip can
# only ever exercise the fallback branch, never a true native-hash
# request/response. This file fakes the paramiko wire primitive instead,
# which is the only way to exercise the native branch at all without a
# real OpenSSH server.


def test_paramiko_checksum_sends_correct_extended_request(monkeypatch):
    import paramiko.sftp as paramiko_sftp

    from pathlib_next.uri.schemes.sftp._paramiko import SftpBackend as _RealSftpBackend

    calls = []

    class _FakeHandleFile:
        def __init__(self):
            self.handle = b"handle-bytes"
            self.closed = False

        def close(self):
            self.closed = True

    class _FakeParamikoClient:
        def __init__(self):
            self.opened = _FakeHandleFile()

        def open(self, path, mode, buffering=-1):
            calls.append(("open", path, mode))
            return self.opened

        def _request(self, cmd, *args):
            calls.append(("_request", cmd, args))
            import paramiko.message as message

            msg = message.Message()
            msg.add_string("md5")
            msg.add_string(bytes.fromhex("deadbeef"))
            msg.rewind()
            return paramiko_sftp.CMD_EXTENDED_REPLY, msg

    backend = _RealSftpBackend.__new__(_RealSftpBackend)
    fake_client = _FakeParamikoClient()
    monkeypatch.setattr(_RealSftpBackend, "client", lambda self, source: fake_client)

    p = _sftp("sftp://host/a.txt", backend=backend)
    result = backend.checksum(p, "md5")

    assert result == "deadbeef"
    assert fake_client.opened.closed is True
    assert calls[0] == ("open", "/a.txt", "r")
    _, cmd, args = calls[1]
    assert cmd == paramiko_sftp.CMD_EXTENDED
    assert args[0] == "check-file@openssh.com"
    assert args[1] == b"handle-bytes"
    assert args[2] == "md5"
    assert int(args[3]) == 0 and int(args[4]) == 0  # offset, length
    assert args[5] == 0  # quick_check


def test_paramiko_checksum_closes_handle_even_when_request_raises(monkeypatch):
    from pathlib_next.uri.schemes.sftp._paramiko import SftpBackend as _RealSftpBackend

    class _FakeHandleFile:
        def __init__(self):
            self.handle = b"h"
            self.closed = False

        def close(self):
            self.closed = True

    class _FakeParamikoClient:
        def __init__(self):
            self.opened = _FakeHandleFile()

        def open(self, path, mode, buffering=-1):
            return self.opened

        def _request(self, cmd, *args):
            raise OSError("SSH_FX_OP_UNSUPPORTED")

    backend = _RealSftpBackend.__new__(_RealSftpBackend)
    fake_client = _FakeParamikoClient()
    monkeypatch.setattr(_RealSftpBackend, "client", lambda self, source: fake_client)

    p = _sftp("sftp://host/a.txt", backend=backend)
    with pytest.raises(OSError):
        backend.checksum(p, "md5")
    assert fake_client.opened.closed is True

    # SftpPath.checksum() (not backend.checksum() directly) is what
    # translates this into NotImplementedError for callers/PathSyncer.
    p2 = _sftp("sftp://host/a.txt", backend=backend)
    with pytest.raises(NotImplementedError):
        p2.checksum()


def test_paramiko_checksum_rejects_mismatched_reply_algorithm(monkeypatch):
    import paramiko.sftp as paramiko_sftp

    from pathlib_next.uri.schemes.sftp._paramiko import SftpBackend as _RealSftpBackend

    class _FakeHandleFile:
        handle = b"h"

        def close(self):
            pass

    class _FakeParamikoClient:
        def open(self, path, mode, buffering=-1):
            return _FakeHandleFile()

        def _request(self, cmd, *args):
            import paramiko.message as message

            msg = message.Message()
            # Server echoes back a different algorithm than requested --
            # must not be trusted.
            msg.add_string("sha256")
            msg.add_string(b"\x00")
            msg.rewind()
            return paramiko_sftp.CMD_EXTENDED_REPLY, msg

    backend = _RealSftpBackend.__new__(_RealSftpBackend)
    monkeypatch.setattr(
        _RealSftpBackend, "client", lambda self, source: _FakeParamikoClient()
    )
    p = _sftp("sftp://host/a.txt", backend=backend)
    with pytest.raises(NotImplementedError):
        backend.checksum(p, "md5")


def test_paramiko_checksum_rejects_non_extended_reply(monkeypatch):
    from pathlib_next.uri.schemes.sftp._paramiko import SftpBackend as _RealSftpBackend

    class _FakeHandleFile:
        handle = b"h"

        def close(self):
            pass

    class _FakeParamikoClient:
        def open(self, path, mode, buffering=-1):
            return _FakeHandleFile()

        def _request(self, cmd, *args):
            import paramiko.message as message

            # A CMD_STATUS-shaped success-ish reply that isn't actually
            # CMD_EXTENDED_REPLY must still be rejected.
            return 999, message.Message()

    backend = _RealSftpBackend.__new__(_RealSftpBackend)
    monkeypatch.setattr(
        _RealSftpBackend, "client", lambda self, source: _FakeParamikoClient()
    )
    p = _sftp("sftp://host/a.txt", backend=backend)
    with pytest.raises(NotImplementedError):
        backend.checksum(p, "md5")


# --- paramiko SftpBackend.supported_checksums(): real per-connection probe -


def test_paramiko_supported_checksums_reflects_working_server(monkeypatch):
    import paramiko.sftp as paramiko_sftp

    from pathlib_next.uri.schemes.sftp import _paramiko as paramiko_module
    from pathlib_next.uri.schemes.sftp._paramiko import SftpBackend as _RealSftpBackend

    class _FakeHandleFile:
        handle = b"h"

        def close(self):
            pass

    class _FakeParamikoClient:
        def open(self, path, mode, buffering=-1):
            return _FakeHandleFile()

        def _request(self, cmd, *args):
            import paramiko.message as message

            msg = message.Message()
            msg.add_string("md5")
            msg.add_string(bytes.fromhex("deadbeef"))
            msg.rewind()
            return paramiko_sftp.CMD_EXTENDED_REPLY, msg

    backend = _RealSftpBackend.__new__(_RealSftpBackend)
    fake_client = _FakeParamikoClient()
    monkeypatch.setattr(_RealSftpBackend, "client", lambda self, source: fake_client)
    # Fresh probe cache -- avoid cross-test pollution from other tests that
    # exercise the same real SftpBackend.checksum()/supported_checksums().
    monkeypatch.setattr(paramiko_module, "_CHECKSUM_SUPPORT_CACHE", {})

    p = _sftp("sftp://host/a.txt", backend=backend)
    supported = backend.supported_checksums(p)

    assert supported == frozenset(paramiko_module._CHECK_FILE_ALGORITHMS)


def test_paramiko_supported_checksums_empty_when_server_lacks_extension(monkeypatch):
    from pathlib_next.uri.schemes.sftp import _paramiko as paramiko_module
    from pathlib_next.uri.schemes.sftp._paramiko import SftpBackend as _RealSftpBackend

    class _FakeHandleFile:
        handle = b"h"

        def close(self):
            pass

    class _FakeParamikoClient:
        def open(self, path, mode, buffering=-1):
            return _FakeHandleFile()

        def _request(self, cmd, *args):
            raise OSError("SSH_FX_OP_UNSUPPORTED")

    backend = _RealSftpBackend.__new__(_RealSftpBackend)
    monkeypatch.setattr(
        _RealSftpBackend, "client", lambda self, source: _FakeParamikoClient()
    )
    monkeypatch.setattr(paramiko_module, "_CHECKSUM_SUPPORT_CACHE", {})

    p = _sftp("sftp://host/a.txt", backend=backend)
    assert backend.supported_checksums(p) == frozenset()


def test_paramiko_supported_checksums_caches_per_connection(monkeypatch):
    import paramiko.sftp as paramiko_sftp

    from pathlib_next.uri.schemes.sftp import _paramiko as paramiko_module
    from pathlib_next.uri.schemes.sftp._paramiko import SftpBackend as _RealSftpBackend

    class _FakeHandleFile:
        handle = b"h"

        def close(self):
            pass

    class _FakeParamikoClient:
        def __init__(self):
            self.request_calls = 0

        def open(self, path, mode, buffering=-1):
            return _FakeHandleFile()

        def _request(self, cmd, *args):
            self.request_calls += 1
            import paramiko.message as message

            msg = message.Message()
            msg.add_string("md5")
            msg.add_string(b"\x00")
            msg.rewind()
            return paramiko_sftp.CMD_EXTENDED_REPLY, msg

    backend = _RealSftpBackend.__new__(_RealSftpBackend)
    fake_client = _FakeParamikoClient()
    monkeypatch.setattr(_RealSftpBackend, "client", lambda self, source: fake_client)
    monkeypatch.setattr(paramiko_module, "_CHECKSUM_SUPPORT_CACHE", {})

    p = _sftp("sftp://host/a.txt", backend=backend)
    backend.supported_checksums(p)
    backend.supported_checksums(p)
    backend.supported_checksums(p)

    # Only the FIRST call actually probed the server -- subsequent calls
    # for the same connection are served from the cache.
    assert fake_client.request_calls == 1


# --- PathSyncer + SFTP: native path used, and fallback still works --------


def test_pathsyncer_uses_sftp_native_checksum_no_open_when_supported():
    from pathlib_next.utils.sync import PathSyncer, SyncEvent

    class _RecordingChecksumBackend(_ChecksumCapableBackend):
        def __init__(self, digests):
            super().__init__(digests=digests)
            self.open_paths = []

        def client(self, source):
            client = super().client(source)
            real_open = client.open

            def _tracking_open(path, mode, buffering):
                self.open_paths.append(path)
                return real_open(path, mode, buffering)

            client.open = _tracking_open
            return client

    source_backend = _RecordingChecksumBackend({"md5": "same-hash"})
    target_backend = _RecordingChecksumBackend({"md5": "same-hash"})
    source = _sftp("sftp://host/a.txt", backend=source_backend)
    target = _sftp("sftp://host/a.txt", backend=target_backend)

    # quick_check=False: this test is specifically about native-checksum
    # preference, not the separately-tested quick_check metadata pre-check
    # (tests/test_checksum.py). "host" doesn't resolve, and whether
    # is_local() treats a non-resolving name as local/non-local is an
    # implementation detail of the resolver chain in use -- isolate this
    # test from that by disabling quick_check outright.
    syncer = PathSyncer(quick_check=False)
    events = []
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)

    assert SyncEvent.Copy not in events
    assert source_backend.calls == [("/a.txt", "md5")]
    assert target_backend.calls == [("/a.txt", "md5")]
    # The whole point of the feature: neither side's file content was
    # opened/streamed just to decide the sync verdict.
    assert source_backend.open_paths == []
    assert target_backend.open_paths == []


def test_pathsyncer_sftp_falls_back_to_streaming_when_backend_unsupported():
    from pathlib_next.utils.sync import PathSyncer

    # _FakeBackend (module-level fixture) has no checksum() override --
    # models a real server without check-file@openssh.com support (or the
    # asyncssh backend). PathSyncer must still reach a correct verdict via
    # the streaming fallback, not silently report "in sync".
    source_backend = _FakeBackend()
    target_backend = _FakeBackend()

    class _OpenableSftpClient(_FakeSftpClient):
        def __init__(self, content: bytes):
            super().__init__()
            self._content = content

        def stat(self, path):
            from pathlib_next.utils.stat import FileStat

            return FileStat(is_dir=False)

        def open(self, path, mode, buffering):
            import io

            return io.BytesIO(self._content)

    source_backend._client = _OpenableSftpClient(b"identical-content")
    target_backend._client = _OpenableSftpClient(b"identical-content")

    source = _sftp("sftp://host/a.txt", backend=source_backend)
    target = _sftp("sftp://host/a.txt", backend=target_backend)

    syncer = PathSyncer()
    events = []
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)

    from pathlib_next.utils.sync import SyncEvent

    assert SyncEvent.Copy not in events
