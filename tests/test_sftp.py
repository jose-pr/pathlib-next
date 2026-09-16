"""Unit-only SFTP tests: mock BaseSftpBackend, no real server. Covers
Source->connect_opts mapping, client cache keying/invalidation, and the
B12/B13 regressions (chmod follow_symlinks, rename target.path).
"""

import os

import pytest

pytest.importorskip("paramiko")

from pathlib_next.uri import Source, Uri
from pathlib_next.uri.schemes.sftp import BaseSftpBackend, SftpBackend, SftpPath


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    # SftpBackend reads ~/.ssh/config and ~/.ssh/known_hosts by default: a
    # developer's own `Host *` / `Port 2200` / ProxyCommand must not change
    # what these tests see. Path.home() reads USERPROFILE on Windows, HOME
    # elsewhere.
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


class _FakeSock:
    def __init__(self, active=True):
        # paramiko's Channel: `active` is set once on open and never
        # cleared; `closed` is what a dropped connection actually sets.
        self.active = active
        self.closed = False


class _FakeAttr:
    def __init__(self, filename, st_mode=0, st_uid=0, st_gid=0):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = 0
        self.st_mtime = 0
        self.st_uid = st_uid
        self.st_gid = st_gid


class _FakeSftpClient:
    def __init__(self):
        self.sock = _FakeSock(True)
        self.close_calls = 0
        self.rename_calls = []
        self.chmod_calls = []
        self.chown_calls = []
        self.symlink_calls = []
        self.link_calls = []
        # Owner reported by stat(), so a partial chown() (one field left
        # "unchanged") has something to read back for the other field.
        self.stat_uid = 501
        self.stat_gid = 20

    def close(self):
        self.close_calls += 1

    def rename(self, path, target):
        self.rename_calls.append((path, target))

    def chmod(self, path, mode):
        self.chmod_calls.append((path, mode))

    def chown(self, path, uid, gid):
        self.chown_calls.append((path, uid, gid))

    def listdir(self, path):
        return ["a", "b"]

    def listdir_attr(self, path):
        return [_FakeAttr("a"), _FakeAttr("b")]

    def stat(self, path):
        return _FakeAttr(path, st_uid=self.stat_uid, st_gid=self.stat_gid)

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

    def symlink(self, target, path):
        self.symlink_calls.append((target, path))

    def link(self, target, path):
        self.link_calls.append((target, path))


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


def test_sftp_backend_client_recreated_when_channel_closed(monkeypatch):
    # A dropped paramiko connection leaves `Channel.active` truthy and sets
    # `closed`; the stale client must be replaced AND closed, not dropped.
    backend = SftpBackend({}, None)
    transport = _FakeTransport()
    monkeypatch.setattr(SftpBackend, "transport", lambda self, source: transport)
    source = Source("sftp", None, "host", None)
    client1 = backend.client(source)
    client1.sock.closed = True
    client2 = backend.client(source)
    assert client2 is not client1
    assert len(transport.clients) == 2
    assert client1.close_calls == 1
    assert client2.close_calls == 0


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

    import paramiko

    mock_ssh = unittest.mock.MagicMock()
    mock_transport = unittest.mock.MagicMock()
    mock_sftp = unittest.mock.MagicMock()

    mock_ssh.get_transport.return_value = mock_transport
    mock_transport.open_sftp_client.return_value = mock_sftp
    # A live channel: MagicMock attributes are truthy, so `closed` would
    # otherwise read as a dropped connection.
    mock_sftp.sock.closed = False

    with unittest.mock.patch("paramiko.SSHClient", return_value=mock_ssh):
        backend = SftpBackend({"timeout": 10}, "policy", ssh_config=None)
        source = Source("sftp", "user:pass", "host", 2222)

        # Test transport()
        transport = backend.transport(source)
        assert transport is mock_transport
        mock_ssh.set_missing_host_key_policy.assert_called_with("policy")
        connect_kwargs = mock_ssh.connect.call_args.kwargs
        assert connect_kwargs["timeout"] == 10
        # connect_opts wins for `timeout`; the other bounds keep the default.
        assert connect_kwargs["banner_timeout"] == SftpBackend.DEFAULT_TIMEOUT
        assert connect_kwargs["auth_timeout"] == SftpBackend.DEFAULT_TIMEOUT
        assert connect_kwargs["hostname"] == "host"
        assert connect_kwargs["port"] == 2222
        assert connect_kwargs["username"] == "user"
        assert connect_kwargs["password"] == "pass"

        # Test client()
        client = backend.client(source)
        assert client is mock_sftp
        mock_transport.open_sftp_client.assert_called_once()

        # Test transport raising if None -- and the client is closed.
        mock_ssh.get_transport.return_value = None
        mock_ssh.close.reset_mock()
        with pytest.raises(paramiko.SSHException, match="no transport"):
            backend.transport(source)
        mock_ssh.close.assert_called_once()


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
# SftpBackend.checksum() speaks the filexfer draft's check-file-handle
# extension directly via paramiko's low-level _request()/CMD_EXTENDED --
# these tests fake that primitive to prove the request is built correctly
# (handle, algorithm, int64 offset/length, block-size) and the reply is
# parsed/validated correctly. Neither OpenSSH nor the asyncssh test server
# (tests/conftest.py::sftp_server) implements the extension, so a
# real-server round trip only ever exercises the fallback branch.

_MD5_DEADBEEF = bytes.fromhex("deadbeef" * 4)


def _check_file_reply(algorithm="md5", digest=_MD5_DEADBEEF, prefixed=False):
    """A draft check-file reply: [string "check-file"] string algorithm,
    then the raw hash bytes as the rest of the packet."""
    import paramiko.message as message

    msg = message.Message()
    if prefixed:
        msg.add_string("check-file")
    msg.add_string(algorithm)
    msg.add_bytes(digest)
    msg.rewind()
    return msg


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
            return paramiko_sftp.CMD_EXTENDED_REPLY, _check_file_reply()

    backend = _RealSftpBackend.__new__(_RealSftpBackend)
    fake_client = _FakeParamikoClient()
    monkeypatch.setattr(_RealSftpBackend, "client", lambda self, source: fake_client)

    p = _sftp("sftp://host/a.txt", backend=backend)
    result = backend.checksum(p, "md5")

    assert result == "deadbeef" * 4
    assert fake_client.opened.closed is True
    assert calls[0] == ("open", "/a.txt", "r")
    _, cmd, args = calls[1]
    assert cmd == paramiko_sftp.CMD_EXTENDED
    # Not "check-file@openssh.com": OpenSSH has no such extension.
    assert args[0] == "check-file-handle"
    assert args[1] == b"handle-bytes"
    assert args[2] == "md5"
    assert int(args[3]) == 0 and int(args[4]) == 0  # start-offset, length
    assert args[5] == 0  # block-size: one hash over the whole file


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
            return paramiko_sftp.CMD_EXTENDED_REPLY, _check_file_reply()

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
            return paramiko_sftp.CMD_EXTENDED_REPLY, _check_file_reply()

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


class _CountingParamikoClient:
    """Fake paramiko client for the check-file tests below: counts opens and
    extension requests, and answers each request with `reply()`."""

    class _HandleFile:
        handle = b"h"

        def close(self):
            pass

    def __init__(self, reply):
        self.reply = reply
        self.opens = 0
        self.requests = 0

    def open(self, path, mode, buffering=-1):
        self.opens += 1
        return self._HandleFile()

    def _request(self, cmd, *args):
        self.requests += 1
        return self.reply()


def _paramiko_backend_with(monkeypatch, client):
    from pathlib_next.uri.schemes.sftp import _paramiko as paramiko_module
    from pathlib_next.uri.schemes.sftp._paramiko import SftpBackend as _RealSftpBackend

    backend = _RealSftpBackend.__new__(_RealSftpBackend)
    monkeypatch.setattr(_RealSftpBackend, "client", lambda self, source: client)
    monkeypatch.setattr(paramiko_module, "_CHECKSUM_SUPPORT_CACHE", {})
    return backend


def test_paramiko_checksum_refusal_is_cached_for_the_connection(monkeypatch):
    # sftp-check-file-openssh-extension-does-not-exist: OpenSSH answers
    # SSH_FX_OP_UNSUPPORTED (paramiko: OSError without errno). Only the
    # first file pays the open + request + close; later ones send nothing.
    def unsupported():
        raise OSError("Operation unsupported")

    client = _CountingParamikoClient(unsupported)
    backend = _paramiko_backend_with(monkeypatch, client)
    p = _sftp("sftp://host/a.txt", backend=backend)

    for _ in range(3):
        with pytest.raises(NotImplementedError):
            p.checksum()

    assert client.requests == 1
    assert client.opens == 1
    assert backend.supported_checksums(p) == frozenset()
    assert client.requests == 1


def test_paramiko_checksum_errno_failure_is_not_cached(monkeypatch):
    # A typed failure (the handle vanished) says nothing about the
    # extension: the next file tries again.
    import errno

    def missing():
        raise OSError(errno.ENOENT, "No such file")

    client = _CountingParamikoClient(missing)
    backend = _paramiko_backend_with(monkeypatch, client)
    p = _sftp("sftp://host/a.txt", backend=backend)

    for _ in range(2):
        with pytest.raises(NotImplementedError):
            p.checksum()
    assert client.requests == 2


def test_paramiko_checksum_accepts_check_file_prefixed_reply(monkeypatch):
    import paramiko.sftp as paramiko_sftp

    client = _CountingParamikoClient(
        lambda: (paramiko_sftp.CMD_EXTENDED_REPLY, _check_file_reply(prefixed=True))
    )
    backend = _paramiko_backend_with(monkeypatch, client)
    p = _sftp("sftp://host/a.txt", backend=backend)
    assert backend.checksum(p, "md5") == "deadbeef" * 4


def test_paramiko_checksum_rejects_digest_of_wrong_length(monkeypatch):
    # A length-prefixed or truncated hash is a reply shape the parser does
    # not understand: never returned as a digest.
    import paramiko.sftp as paramiko_sftp

    client = _CountingParamikoClient(
        lambda: (
            paramiko_sftp.CMD_EXTENDED_REPLY,
            _check_file_reply(digest=bytes.fromhex("deadbeef")),
        )
    )
    backend = _paramiko_backend_with(monkeypatch, client)
    p = _sftp("sftp://host/a.txt", backend=backend)
    with pytest.raises(NotImplementedError):
        backend.checksum(p, "md5")


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


def test_pathsyncer_sftp_falls_back_to_streaming_when_backend_unsupported(
    monkeypatch,
):
    import netimps

    from pathlib_next.utils.sync import PathSyncer

    # PathSyncer asks `is_local()` of "sftp://host", which resolves "host":
    # answer "does not resolve" without a real DNS query.
    monkeypatch.setattr(netimps, "resolve", lambda *args, **kwargs: [])

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


# --- chown: sentinel normalization (2026-08-04 finding) -------------------
#
# SFTPv3's setstat sends uid/gid as a *paired* attribute, so there is no way
# to set only one. SftpPath._chown() therefore reads the current owner for
# whichever field the caller left as "unchanged" -- these pin that, since a
# backend that silently sent 0 (or -1) instead would chown to root.


def test_chown_sets_both_fields():
    backend = _FakeBackend()
    p = _sftp("sftp://host/a.txt", backend=backend)
    p.chown(1000, 1000)
    assert backend._client.chown_calls == [("/a.txt", 1000, 1000)]


def test_chown_uid_only_reads_current_gid():
    backend = _FakeBackend()
    backend._client.stat_uid, backend._client.stat_gid = 501, 20
    p = _sftp("sftp://host/a.txt", backend=backend)
    p.chown(uid=1000)
    # gid must come from stat(), not a 0/-1 guess.
    assert backend._client.chown_calls == [("/a.txt", 1000, 20)]


def test_chown_gid_only_reads_current_uid():
    backend = _FakeBackend()
    backend._client.stat_uid, backend._client.stat_gid = 501, 20
    p = _sftp("sftp://host/a.txt", backend=backend)
    p.chown(gid=1000)
    assert backend._client.chown_calls == [("/a.txt", 501, 1000)]


def test_chown_minus_one_is_the_unchanged_sentinel():
    backend = _FakeBackend()
    backend._client.stat_uid, backend._client.stat_gid = 501, 20
    p = _sftp("sftp://host/a.txt", backend=backend)
    # -1 is os.chown's spelling of "leave alone"; it must mean the same
    # thing here rather than reaching the wire as a literal -1.
    p.chown(-1, 1000)
    assert backend._client.chown_calls == [("/a.txt", 501, 1000)]


def test_chown_all_unchanged_is_a_no_op():
    backend = _FakeBackend()
    p = _sftp("sftp://host/a.txt", backend=backend)
    p.chown()
    p.chown(-1, -1)
    assert backend._client.chown_calls == []


def test_chown_uid_zero_is_not_treated_as_unset():
    backend = _FakeBackend()
    p = _sftp("sftp://host/a.txt", backend=backend)
    # uid 0 is root -- a falsy check instead of an `is None` check would
    # silently drop this and leave the file's owner untouched.
    p.chown(0, 0)
    assert backend._client.chown_calls == [("/a.txt", 0, 0)]


def test_chown_names_not_supported_over_sftp():
    p = _sftp("sftp://host/a.txt")
    with pytest.raises(NotImplementedError):
        p.chown("root", "wheel")


def test_chown_follow_symlinks_false_raises_notimplemented():
    p = _sftp("sftp://host/a.txt")
    with pytest.raises(NotImplementedError):
        p.chown(1000, 1000, follow_symlinks=False)


# --- chmod: string octal mode (2026-08-04 finding) ------------------------


def test_chmod_accepts_string_octal_mode():
    backend = _FakeBackend()
    p = _sftp("sftp://host/a.txt", backend=backend)
    p.chmod("0644")
    p.chmod("644")
    # Both spellings must reach the wire as the same int the octal literal
    # means -- decimal 644 (0o1204) would be a different, valid mode.
    assert backend._client.chmod_calls == [("/a.txt", 0o644), ("/a.txt", 0o644)]


def test_chmod_rejects_non_octal_digits():
    p = _sftp("sftp://host/a.txt")
    with pytest.raises(ValueError):
        p.chmod("0899")


# --- destination arguments are decoded paths, not URI syntax (0.9.3) ------
#
# `rename()`/`symlink_to()` used to run a `str` destination back through the
# URI parser (`Uri(self.parent, target)` / `type(self)(target)`), so anything
# from a "?" or "#" onward was discarded and "%xx" was decoded -- silently,
# and against a real server (measured on TrueNAS 26.0.0-BETA.1):
#     rename(".../rn?b.txt")      -> the file became ".../rn"
#     symlink_to(".../cache?v=2") -> the link pointed at ".../cache"
# Every assertion below is on the argument the BACKEND received, not on the
# absence of an exception: the buggy code raised nothing at all.

# ("filename", "the path the wire call must carry for /mnt/<filename>")
_DEST_NAMES = [
    "rn?b.txt",  # query delimiter
    "rn#b.txt",  # fragment delimiter
    "rn b.txt",  # space (already survived, guards the fix)
    "rn%20b.txt",  # LITERAL percent-escape: must NOT become "rn b.txt"
    "rn%b.txt",  # bare, un-decodable percent
    "rn:b.txt",  # colon must stay readable (a "/C:/Temp" path depends on it)
    "a+b&c=d.txt",  # other sub-delims
]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_rename_str_destination_is_a_literal_path(name):
    backend = _FakeBackend()
    p = _sftp("sftp://host/mnt/a.txt", backend=backend)
    p.rename(name)
    # Relative str == sibling rename, so it lands beside self.
    assert backend._client.rename_calls == [("/mnt/a.txt", f"/mnt/{name}")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_rename_absolute_str_destination_is_a_literal_path(name):
    backend = _FakeBackend()
    p = _sftp("sftp://host/mnt/a.txt", backend=backend)
    p.rename(f"/other/{name}")
    assert backend._client.rename_calls == [("/mnt/a.txt", f"/other/{name}")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_symlink_to_str_target_is_a_literal_path(name):
    backend = _FakeBackend()
    p = _sftp("sftp://host/mnt/link", backend=backend)
    p.symlink_to(f"/mnt/{name}")
    assert backend._client.symlink_calls == [(f"/mnt/{name}", "/mnt/link")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_symlink_to_relative_str_target_stays_relative_and_literal(name):
    backend = _FakeBackend()
    p = _sftp("sftp://host/mnt/link", backend=backend)
    p.symlink_to(name)
    # Unlike rename()'s destination, a symlink target is stored verbatim --
    # never anchored at self.parent (pathlib.Path.symlink_to() parity).
    assert backend._client.symlink_calls == [(name, "/mnt/link")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_hardlink_to_str_target_is_a_literal_path(name):
    class _HardlinkBackend(_FakeBackend):
        supports_hardlink = True

    backend = _HardlinkBackend()
    p = _sftp("sftp://host/mnt/link", backend=backend)
    p.hardlink_to(f"/mnt/{name}")
    assert backend._client.link_calls == [(f"/mnt/{name}", "/mnt/link")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_rename_path_object_destination_is_not_double_decoded(name):
    """A destination that is already a path OBJECT must reach the wire
    exactly once-decoded.

    This is the double-encoding guard: downstream consumers
    percent-encode a decoded filesystem path and construct the path from
    the resulting URI. The fix must not add a second encode/decode round
    on top -- a literal "%20" in the name is the case that catches it.
    """
    import uritools

    # RFC 3986 pchar + "/" -- ":" deliberately left unencoded so that a
    # "/C:/Temp/..." path stays readable in the URI.
    encoded = uritools.uriencode(f"/mnt/{name}", safe="/:@-._~!$&'()*+,;=").decode()
    backend = _FakeBackend()
    p = _sftp("sftp://host/mnt/a.txt", backend=backend)
    target = SftpPath(f"sftp://host{encoded}", backend=backend)
    assert target.path == f"/mnt/{name}"
    p.rename(target)
    assert backend._client.rename_calls == [("/mnt/a.txt", f"/mnt/{name}")]


@pytest.mark.parametrize("name", _DEST_NAMES)
def test_symlink_to_path_object_target_is_not_double_decoded(name):
    import uritools

    encoded = uritools.uriencode(f"/mnt/{name}", safe="/:@-._~!$&'()*+,;=").decode()
    backend = _FakeBackend()
    p = _sftp("sftp://host/mnt/link", backend=backend)
    target = SftpPath(f"sftp://host{encoded}", backend=backend)
    p.symlink_to(target)
    assert backend._client.symlink_calls == [(f"/mnt/{name}", "/mnt/link")]


def test_rename_str_destination_keeps_a_windows_style_drive_path():
    # A relative destination whose first segment ends in ":" used to be
    # read as a URI SCHEME: "C:/Temp/x.txt" parsed as scheme "c" with path
    # "/Temp/x.txt", so the rename left the directory entirely.
    backend = _FakeBackend()
    p = _sftp("sftp://host/mnt/a.txt", backend=backend)
    p.rename("C:/Temp/x.txt")
    assert backend._client.rename_calls == [("/mnt/a.txt", "/mnt/C:/Temp/x.txt")]


def test_symlink_to_str_target_keeps_dot_dot_relative():
    backend = _FakeBackend()
    p = _sftp("sftp://host/mnt/sub/link", backend=backend)
    p.symlink_to("../real.txt")
    assert backend._client.symlink_calls == [("../real.txt", "/mnt/sub/link")]


# --- wave 5 (G6): rename / unlink / readlink parity -------------------------


class _LinkAndRenameClient(_FakeSftpClient):
    """Fake client with a tiny namespace: `entries` maps a path to "file" or
    ("link", target); stat() follows links, lstat() does not."""

    def __init__(self, entries, *, posix_rename=True, posix_error=None):
        super().__init__()
        self.entries = dict(entries)
        self.posix_rename_calls = []
        self.remove_calls = []
        self._posix_error = posix_error
        if not posix_rename:
            self.posix_rename = None

    def _attr(self, path):
        import stat as stat_module

        from pathlib_next.utils.stat import FileStat

        entry = self.entries.get(path)
        if entry is None:
            raise FileNotFoundError(2, "No such file", path)
        if isinstance(entry, tuple):
            return FileStat(st_mode=stat_module.S_IFLNK | 0o777)
        return FileStat(st_mode=stat_module.S_IFREG | 0o644)

    def lstat(self, path):
        return self._attr(path)

    def stat(self, path):
        entry = self.entries.get(path)
        if isinstance(entry, tuple):
            return self._attr(entry[1])
        return self._attr(path)

    def remove(self, path):
        self.remove_calls.append(path)
        if path not in self.entries:
            raise FileNotFoundError(2, "No such file", path)
        del self.entries[path]

    def readlink(self, path):
        return self.entries[path][1]

    def posix_rename(self, path, target):
        self.posix_rename_calls.append((path, target))
        if self._posix_error is not None:
            raise self._posix_error
        self.entries[target] = self.entries.pop(path)

    def rename(self, path, target):
        self.rename_calls.append((path, target))
        if target in self.entries:
            raise OSError("Failure")
        self.entries[target] = self.entries.pop(path)


def _link_backend(client):
    backend = _FakeBackend()
    backend._client = client
    return backend


def test_unlink_missing_ok_removes_dangling_link_without_exists_probe():
    client = _LinkAndRenameClient({"/current": ("link", "/releases/41")})
    link = _sftp("sftp://host/current", backend=_link_backend(client))
    assert not link.exists()

    link.unlink(missing_ok=True)

    assert client.remove_calls == ["/current"]
    assert "/current" not in client.entries


def test_unlink_missing_ok_ignores_only_a_missing_entry():
    client = _LinkAndRenameClient({})
    path = _sftp("sftp://host/gone", backend=_link_backend(client))
    path.unlink(missing_ok=True)
    with pytest.raises(FileNotFoundError):
        path.unlink()


def test_symlink_to_force_repoints_a_dangling_link():
    client = _LinkAndRenameClient({"/current": ("link", "/releases/41")})
    link = _sftp("sftp://host/current", backend=_link_backend(client))

    link.symlink_to("/releases/42", force=True)

    assert client.symlink_calls == [("/releases/42", "/current")]


def test_rename_uses_posix_rename_to_replace_existing_target():
    client = _LinkAndRenameClient({"/a.txt": "file", "/b.txt": "file"})
    path = _sftp("sftp://host/a.txt", backend=_link_backend(client))

    path.rename("b.txt")

    assert client.posix_rename_calls == [("/a.txt", "/b.txt")]
    assert client.rename_calls == []


def test_rename_without_posix_rename_raises_file_exists_error():
    import errno

    client = _LinkAndRenameClient(
        {"/a.txt": "file", "/b.txt": "file"}, posix_rename=False
    )
    path = _sftp("sftp://host/a.txt", backend=_link_backend(client))

    with pytest.raises(FileExistsError) as excinfo:
        path.rename("b.txt")

    assert excinfo.value.errno == errno.EEXIST
    assert client.entries == {"/a.txt": "file", "/b.txt": "file"}


def test_rename_falls_back_when_posix_rename_is_unsupported():
    # asyncssh: NotImplementedError (the extension was not advertised).
    client = _LinkAndRenameClient(
        {"/a.txt": "file"}, posix_error=NotImplementedError("unsupported")
    )
    path = _sftp("sftp://host/a.txt", backend=_link_backend(client))

    path.rename("c.txt")

    assert client.rename_calls == [("/a.txt", "/c.txt")]
    assert "/c.txt" in client.entries


def test_rename_paramiko_generic_failure_is_learned_as_unsupported():
    # paramiko renders SSH_FX_OP_UNSUPPORTED as OSError without errno: once a
    # plain rename then succeeds, later renames skip the extension request.
    client = _LinkAndRenameClient(
        {"/a.txt": "file", "/b.txt": "file"},
        posix_error=OSError("Operation unsupported"),
    )
    backend = _link_backend(client)

    _sftp("sftp://host/a.txt", backend=backend).rename("c.txt")
    _sftp("sftp://host/b.txt", backend=backend).rename("d.txt")

    assert client.posix_rename_calls == [("/a.txt", "/c.txt")]
    assert client.rename_calls == [("/a.txt", "/c.txt"), ("/b.txt", "/d.txt")]


def test_rename_errno_failure_from_posix_rename_propagates():
    client = _LinkAndRenameClient(
        {"/a.txt": "file"}, posix_error=PermissionError(13, "denied")
    )
    path = _sftp("sftp://host/a.txt", backend=_link_backend(client))
    with pytest.raises(PermissionError):
        path.rename("c.txt")
    assert client.rename_calls == []


def test_readlink_relative_target_is_printable_and_comparable():
    client = _LinkAndRenameClient({"/srv/app/current": ("link", "releases/42")})
    link = _sftp("sftp://host/srv/app/current", backend=_link_backend(client))

    target = link.readlink()

    assert target.path == "releases/42"
    assert str(target) == "releases/42"
    assert "releases/42" in repr(target)
    assert target == link.readlink()
    assert target.name == "42"


def test_readlink_keeps_dot_segments_verbatim():
    client = _LinkAndRenameClient({"/srv/current": ("link", "../x/./y")})
    link = _sftp("sftp://host/srv/current", backend=_link_backend(client))
    assert link.readlink().path == "../x/./y"


def test_readlink_absolute_target_keeps_the_host():
    client = _LinkAndRenameClient({"/srv/current": ("link", "/releases/42")})
    link = _sftp("sftp://host/srv/current", backend=_link_backend(client))

    target = link.readlink()

    assert target.path == "/releases/42"
    assert target.source == link.source


# --- a listing is untrusted input -----------------------------------------


#: What a malicious or buggy server can put in a directory listing. Each
#: would escape the directory being walked if it became a child path.
_HOSTILE_LISTING_NAMES = ["..", ".", "../victim.txt", "sub/../../victim.txt", ""]


def _client_listing(names):
    class _HostileClient(_FakeSftpClient):
        def listdir(self, path):
            return list(names)

        def listdir_attr(self, path):
            return [_FakeAttr(name) for name in names]

    class _HostileBackend(_FakeBackend):
        def __init__(self):
            super().__init__()
            self._client = _HostileClient()

    return _HostileBackend()


def test_a_hostile_listing_name_never_becomes_a_child():
    """`rm(recursive=True)` and a recursive copy walk what the LISTING says
    is there. A server-chosen `../victim.txt` used to become a real path
    outside the tree -- `dav:`/`http:` already filtered, `sftp:` did not.
    The destination-side guard cannot catch it: by then the name has
    collapsed to a harmless-looking `victim.txt`."""
    backend = _client_listing(_HOSTILE_LISTING_NAMES + ["ok.txt"])
    directory = SftpPath("sftp://host/src", backend=backend)

    assert [p.name for p in directory.iterdir()] == ["ok.txt"]
    assert [name for name, _stat in directory._scandir()] == ["ok.txt"]
    # Nothing yielded can leave the directory it was listed from.
    for child in directory.iterdir():
        assert child.path.startswith("/src/")
        assert ".." not in child.path.split("/")


def test_a_listing_name_with_a_separator_is_dropped():
    """A name carrying "/" is two components, not one: joining it would
    reach a path the listing does not describe."""
    backend = _client_listing(["a/b", "ok.txt"])
    directory = SftpPath("sftp://host/src", backend=backend)
    assert [p.name for p in directory.iterdir()] == ["ok.txt"]
