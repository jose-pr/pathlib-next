"""Unit-only asyncssh SFTP backend tests: error translation, the SFTPAttrs
-> st_*-shaped stat adapter, and backend-selection precedence. Mirrors
test_sftp.py's paramiko coverage for the pieces that are asyncssh-specific;
end-to-end behavior (real read/write/mkdir/rename/... against a live
server) is covered by TestSftpContract's "asyncssh" param in
test_contract.py.
"""
import stat
from types import SimpleNamespace

import pytest

asyncssh = pytest.importorskip("asyncssh")

from pathlib_next.uri import Source
from pathlib_next.uri.schemes.sftp import _asyncssh as backend_mod


# --- error translation -------------------------------------------------


@pytest.mark.parametrize(
    "error, expected_type",
    [
        (asyncssh.SFTPNoSuchFile("no such file"), FileNotFoundError),
        (asyncssh.SFTPNoSuchPath("no such path"), FileNotFoundError),
        (asyncssh.SFTPFileAlreadyExists("exists"), FileExistsError),
        (asyncssh.SFTPPermissionDenied("denied"), PermissionError),
        (asyncssh.SFTPOpUnsupported("unsupported"), NotImplementedError),
        (asyncssh.SFTPFailure("generic v3 failure"), OSError),
    ],
)
def test_translate_maps_typed_errors(error, expected_type):
    result = backend_mod._translate(error)
    assert isinstance(result, expected_type)


def test_translate_dir_not_empty_sets_enotempty_errno():
    import errno

    result = backend_mod._translate(asyncssh.SFTPDirNotEmpty("not empty"))
    assert isinstance(result, OSError)
    assert result.errno == errno.ENOTEMPTY


def test_reraise_sftp_errors_translates_and_chains():
    @backend_mod._reraise_sftp_errors
    def _raises():
        raise asyncssh.SFTPNoSuchFile("gone")

    with pytest.raises(FileNotFoundError) as excinfo:
        _raises()
    assert isinstance(excinfo.value.__cause__, asyncssh.SFTPNoSuchFile)


def test_reraise_sftp_errors_passes_through_other_exceptions():
    @backend_mod._reraise_sftp_errors
    def _raises():
        raise ValueError("unrelated")

    with pytest.raises(ValueError):
        _raises()


# --- stat adapter --------------------------------------------------------


def _attrs(**kwargs):
    return asyncssh.SFTPAttrs(**kwargs)


def test_stat_adapter_v3_style_combined_permissions():
    # v3 servers (and asyncssh's own bundled SFTPServer, verified
    # empirically) pack S_IFMT type bits directly into `.permissions`.
    attrs = _attrs(type=asyncssh.FILEXFER_TYPE_REGULAR, permissions=0o100644, size=42)
    adapted = backend_mod._StatAdapter(attrs)
    assert adapted.st_mode == 0o100644
    assert stat.S_ISREG(adapted.st_mode)
    assert adapted.st_size == 42


def test_stat_adapter_v4_style_bare_permissions_combined_with_type():
    # Defensive path: a genuine v4+ server that reports only the bare
    # permission bits, with the type carried separately in `.type`.
    attrs = _attrs(type=asyncssh.FILEXFER_TYPE_DIRECTORY, permissions=0o755)
    adapted = backend_mod._StatAdapter(attrs)
    assert stat.S_ISDIR(adapted.st_mode)
    assert stat.S_IMODE(adapted.st_mode) == 0o755


def test_stat_adapter_symlink_type_bit():
    attrs = _attrs(type=asyncssh.FILEXFER_TYPE_SYMLINK, permissions=0o120777)
    adapted = backend_mod._StatAdapter(attrs)
    assert stat.S_ISLNK(adapted.st_mode)


def test_stat_adapter_none_fields_default_to_zero_not_none():
    # SFTPAttrs' numeric fields can be None (not just absent) -- a naive
    # `getattr(attrs, 'size', 0)` would return None here, not 0.
    attrs = _attrs()
    adapted = backend_mod._StatAdapter(attrs)
    assert adapted.st_size == 0
    assert adapted.st_uid == 0
    assert adapted.st_gid == 0
    assert adapted.st_nlink == 1
    assert adapted.st_atime == 0
    assert adapted.st_mtime == 0
    assert adapted.st_ctime == 0


def test_stat_adapter_filename_from_readdir_entry():
    attrs = _attrs(type=asyncssh.FILEXFER_TYPE_REGULAR, permissions=0o100644)
    adapted = backend_mod._StatAdapter(attrs, filename="a.txt")
    assert adapted.filename == "a.txt"


def test_filestat_from_stat_adapter_roundtrip():
    from pathlib_next.utils.stat import FileStat

    attrs = _attrs(type=asyncssh.FILEXFER_TYPE_DIRECTORY, permissions=0o40755, size=0)
    adapted = backend_mod._StatAdapter(attrs)
    fs = FileStat.from_stat(adapted)
    assert fs.is_dir()
    assert not fs.is_file()


# --- backend selection ----------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_backend_resolution(monkeypatch):
    # _resolve_default_backend_cls() caches its result module-globally --
    # isolate each test from that cache and from the real process env.
    monkeypatch.delenv(sftp_pkg.__dict__.get("_ENV_VAR", "PATHLIB_NEXT_SFTP_BACKEND"), raising=False)
    yield


from pathlib_next.uri.schemes import sftp as sftp_pkg  # noqa: E402


def test_resolve_default_backend_auto_prefers_asyncssh(monkeypatch):
    monkeypatch.delenv(sftp_pkg._ENV_VAR, raising=False)
    cls = sftp_pkg._resolve_default_backend_cls(reload=True)
    assert cls is backend_mod.AsyncsshSftpBackend


def test_resolve_default_backend_explicit_paramiko(monkeypatch):
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "paramiko")
    cls = sftp_pkg._resolve_default_backend_cls(reload=True)
    assert cls is sftp_pkg.SftpBackend


def test_resolve_default_backend_explicit_asyncssh(monkeypatch):
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "asyncssh")
    cls = sftp_pkg._resolve_default_backend_cls(reload=True)
    assert cls is backend_mod.AsyncsshSftpBackend


def test_resolve_default_backend_invalid_value_raises(monkeypatch):
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "not-a-backend")
    with pytest.raises(ValueError, match="not-a-backend"):
        sftp_pkg._resolve_default_backend_cls(reload=True)


def test_resolve_default_backend_asyncssh_unavailable_raises_importerror(monkeypatch):
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "asyncssh")
    monkeypatch.setitem(sftp_pkg._BACKEND_REGISTRY, "paramiko", sftp_pkg.SftpBackend)
    monkeypatch.delitem(sftp_pkg._BACKEND_REGISTRY, "asyncssh", raising=False)
    monkeypatch.setattr(sftp_pkg, "_asyncssh_probed", True)  # skip the real probe
    with pytest.raises(ImportError, match="sftp-async"):
        sftp_pkg._resolve_default_backend_cls(reload=True)


def test_resolve_default_backend_result_is_cached(monkeypatch):
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "paramiko")
    first = sftp_pkg._resolve_default_backend_cls(reload=True)
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "asyncssh")
    second = sftp_pkg._resolve_default_backend_cls()  # no reload -- cached
    assert first is second is sftp_pkg.SftpBackend


def test_default_backend_cls_class_attribute_wins_over_env(monkeypatch):
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "asyncssh")

    class _PinnedSftpPath(sftp_pkg.SftpPath):
        _default_backend_cls = sftp_pkg.SftpBackend
        __SCHEMES = ()  # avoid registering a second sftp: scheme

    inst = _PinnedSftpPath.__new__(_PinnedSftpPath)
    backend = inst._initbackend()
    assert isinstance(backend, sftp_pkg.SftpBackend)


def test_explicit_backend_kwarg_wins_over_everything(monkeypatch):
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "asyncssh")
    explicit = backend_mod.AsyncsshSftpBackend()
    p = sftp_pkg.SftpPath("sftp://host/a", backend=explicit)
    assert p.backend is explicit


def test_asyncssh_getattr_is_lazy_and_reexports():
    # PEP 562 module __getattr__ -- accessing the name imports _asyncssh
    # lazily; already imported here via backend_mod, so this just checks
    # the re-export resolves to the same class.
    assert sftp_pkg.AsyncsshSftpBackend is backend_mod.AsyncsshSftpBackend


def test_asyncssh_getattr_unknown_name_raises_attributeerror():
    with pytest.raises(AttributeError):
        sftp_pkg.__getattr__("NotARealAttribute")


# --- capability flags -------------------------------------------------


def test_asyncssh_backend_supports_lchmod_and_hardlink():
    backend = backend_mod.AsyncsshSftpBackend()
    assert backend.supports_lchmod is True
    assert backend.supports_hardlink is True


def test_paramiko_backend_does_not_support_lchmod_or_hardlink():
    assert sftp_pkg.SftpBackend.supports_lchmod is False
    assert sftp_pkg.SftpBackend.supports_hardlink is False


def test_hardlink_to_raises_immediately_on_paramiko_no_round_trip(monkeypatch):
    calls = []

    class _FakeParamikoBackend(sftp_pkg.BaseSftpBackend):
        def client(self, source):
            calls.append(source)
            raise AssertionError("client() should not be called")

    p = sftp_pkg.SftpPath("sftp://host/a", backend=_FakeParamikoBackend())
    with pytest.raises(NotImplementedError):
        p.hardlink_to("sftp://host/b")
    assert calls == []


# --- concurrent copy tests -----------------------------------------------


def test_asyncssh_backend_has_max_concurrency():
    backend = backend_mod.AsyncsshSftpBackend(max_concurrency=16)
    assert backend.max_concurrency == 16


def test_asyncssh_backend_max_concurrency_defaults_to_8():
    backend = backend_mod.AsyncsshSftpBackend()
    assert backend.max_concurrency == 8
    assert "config" not in backend.connect_opts


def test_asyncssh_backend_accepts_connect_opts_and_sftp_version():
    backend = backend_mod.AsyncsshSftpBackend(
        {"config": None, "client_keys": None},
        max_concurrency=12,
        sftp_version=3,
    )
    assert backend.connect_opts == {"config": None, "client_keys": None}
    assert backend.max_concurrency == 12
    assert backend.sftp_version == 3


def test_asyncssh_backend_ssh_config_kwarg_is_backend_agnostic():
    backend = backend_mod.AsyncsshSftpBackend(ssh_config=None)
    assert backend.connect_opts["config"] is None
    backend = backend_mod.AsyncsshSftpBackend(ssh_config=("a", "b"))
    assert backend.connect_opts["config"] == ("a", "b")


def test_aconnect_merges_source_credentials_and_connect_opts(monkeypatch):
    import asyncio

    calls = {}

    class _FakeConn:
        async def start_sftp_client(self, *, sftp_version):
            calls["sftp_version"] = sftp_version
            return object()

    async def _fake_connect(host, port, **kwargs):
        calls["host"] = host
        calls["port"] = port
        calls["kwargs"] = kwargs
        return _FakeConn()

    monkeypatch.setattr(backend_mod._asyncssh, "connect", _fake_connect)

    entry = asyncio.run(
        backend_mod._aconnect(
            Source("sftp", "user:pass", "host", 2222),
            connect_opts={
                "config": None,
                "client_keys": None,
                "agent_path": None,
                "username": "ignored",
            },
            sftp_version=4,
        )
    )
    assert isinstance(entry.client, backend_mod._SyncSftpClient)
    assert calls["host"] == "host"
    assert calls["port"] == 2222
    assert calls["kwargs"]["config"] is None
    assert calls["kwargs"]["client_keys"] is None
    assert calls["kwargs"]["agent_path"] is None
    assert calls["kwargs"]["known_hosts"] is None
    assert calls["kwargs"]["username"] == "user"
    assert calls["kwargs"]["password"] == "pass"
    assert calls["sftp_version"] == 4


class _FakeAsyncCopyFile:
    def __init__(self, client, path, mode):
        self.client = client
        self.path = path
        self.mode = mode

    async def read(self, size=-1):
        data = self.client.files[self.path]
        self.client.files[self.path] = b""
        return data

    async def write(self, data):
        self.client.files[self.path] = self.client.files.get(self.path, b"") + data

    async def close(self):
        pass


class _FakeAsyncCopyClient:
    def __init__(self, *, fail_paths=frozenset(), delay=0):
        self.files = {
            "/src/a.txt": b"a",
            "/src/b.txt": b"b",
            "/src/c.txt": b"c",
            "/src/d.txt": b"d",
        }
        self.dirs = {"/src": ["a.txt", "b.txt", "c.txt", "d.txt"], "/dst": []}
        self.fail_paths = set(fail_paths)
        self.delay = delay
        self.active = 0
        self.max_active = 0

    async def _enter(self, path=None):
        import asyncio

        if path in self.fail_paths:
            raise asyncssh.SFTPFailure(path)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.delay:
            await asyncio.sleep(self.delay)

    async def _exit(self):
        self.active -= 1

    async def stat(self, path):
        await self._enter(path)
        try:
            if path in self.dirs:
                return _sftp_attrs(True)
            if path in self.files:
                return _sftp_attrs(False)
            raise asyncssh.SFTPNoSuchFile(path)
        finally:
            await self._exit()

    async def lstat(self, path):
        return await self.stat(path)

    async def readdir(self, path):
        await self._enter(path)
        try:
            return [SimpleNamespace(filename=name) for name in self.dirs[path]]
        finally:
            await self._exit()

    async def mkdir(self, path, attrs):
        await self._enter(path)
        try:
            self.dirs[path] = []
        finally:
            await self._exit()

    async def remove(self, path):
        await self._enter(path)
        try:
            self.files.pop(path, None)
        finally:
            await self._exit()

    async def chmod(self, path, mode):
        await self._enter(path)
        await self._exit()

    async def open(self, path, mode, encoding=None):
        await self._enter(path)
        try:
            if "w" in mode:
                self.files[path] = b""
            return _FakeAsyncCopyFile(self, path, mode)
        finally:
            await self._exit()


class _FakeAsyncCopyPath:
    def __init__(self, client, path):
        self.path = path
        self.name = path.rstrip("/").rsplit("/", 1)[-1]
        self._sftpclient = SimpleNamespace(_aclient=client)

    def __truediv__(self, name):
        return type(self)(self._sftpclient._aclient, f"{self.path}/{name}")

    def iterdir(self):
        return [self / name for name in self._sftpclient._aclient.dirs[self.path]]


def test_concurrent_copy_native_respects_max_concurrency():
    import asyncio

    client = _FakeAsyncCopyClient(delay=0.01)
    asyncio.run(
        backend_mod._concurrent_copy(
            _FakeAsyncCopyPath(client, "/src"),
            _FakeAsyncCopyPath(client, "/dst"),
            overwrite=False,
            follow_symlinks=True,
            preserve_metadata=True,
            max_concurrency=3,
            ignore_error=None,
        )
    )
    assert client.files["/dst/a.txt"] == b"a"
    assert client.files["/dst/d.txt"] == b"d"
    assert 1 < client.max_active <= 3


def test_concurrent_copy_ignore_error_allows_partial_failure():
    import asyncio

    client = _FakeAsyncCopyClient(fail_paths={"/src/a.txt", "/src/c.txt"})
    errors = []
    asyncio.run(
        backend_mod._concurrent_copy(
            _FakeAsyncCopyPath(client, "/src"),
            _FakeAsyncCopyPath(client, "/dst"),
            overwrite=False,
            follow_symlinks=True,
            preserve_metadata=True,
            max_concurrency=4,
            ignore_error=errors.append,
        )
    )
    assert sorted(path for path in client.files if path.startswith("/dst/")) == [
        "/dst/b.txt",
        "/dst/d.txt",
    ]
    assert len(errors) == 2
    assert all(isinstance(error, OSError) for error in errors)


def test_concurrent_copy_fail_fast_cancels_queued_children():
    import asyncio

    client = _FakeAsyncCopyClient(fail_paths={"/src/a.txt"}, delay=0.01)
    with pytest.raises(OSError):
        asyncio.run(
            backend_mod._concurrent_copy(
                _FakeAsyncCopyPath(client, "/src"),
                _FakeAsyncCopyPath(client, "/dst"),
                overwrite=False,
                follow_symlinks=True,
                preserve_metadata=True,
                max_concurrency=1,
                ignore_error=None,
            )
        )
    assert len([path for path in client.files if path.startswith("/dst/")]) < 4


def test_sftppath_copy_recursive_uses_concurrent_helper(monkeypatch):
    import asyncio

    recorded = {}

    async def _fake_concurrent_copy(path, target, **kwargs):
        recorded["path"] = path
        recorded["target"] = target
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(backend_mod, "_concurrent_copy", _fake_concurrent_copy)
    monkeypatch.setattr(backend_mod, "_run", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(sftp_pkg.SftpPath, "is_dir", lambda self: True)

    src = sftp_pkg.SftpPath(
        "sftp://host/src", backend=backend_mod.AsyncsshSftpBackend(max_concurrency=5)
    )
    target = sftp_pkg.SftpPath("sftp://host/dst", backend=src.backend)
    monkeypatch.setattr(type(target), "exists", lambda self: False)
    monkeypatch.setattr(type(target), "mkdir", lambda self, mode=0o777, parents=False, exist_ok=False: None)

    src.copy(target, recursive=True, overwrite=True)

    assert recorded["path"] is src
    assert recorded["target"] is target
    assert recorded["kwargs"]["max_concurrency"] == 5


# --- concurrent remove tests ---------------------------------------------


def _sftp_attrs(is_dir):
    permissions = 0o40755 if is_dir else 0o100644
    file_type = asyncssh.FILEXFER_TYPE_DIRECTORY if is_dir else asyncssh.FILEXFER_TYPE_REGULAR
    return asyncssh.SFTPAttrs(type=file_type, permissions=permissions)


class _FakeAsyncRmClient:
    def __init__(self, tree, *, delay=0, fail_remove=frozenset()):
        self.tree = tree
        self.delay = delay
        self.fail_remove = set(fail_remove)
        self.active = 0
        self.max_active = 0
        self.removed = []
        self.rmdirs = []

    async def _enter(self):
        import asyncio

        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.delay:
            await asyncio.sleep(self.delay)

    async def _exit(self):
        self.active -= 1

    async def stat(self, path):
        await self._enter()
        try:
            if path not in self.tree:
                raise asyncssh.SFTPNoSuchFile(path)
            return _sftp_attrs(self.tree[path] is not None)
        finally:
            await self._exit()

    async def lstat(self, path):
        return await self.stat(path)

    async def readdir(self, path):
        await self._enter()
        try:
            children = self.tree[path] or []
            return [SimpleNamespace(filename=name) for name in children]
        finally:
            await self._exit()

    async def remove(self, path):
        await self._enter()
        try:
            if path in self.fail_remove:
                raise asyncssh.SFTPFailure(path)
            self.removed.append(path)
        finally:
            await self._exit()

    async def rmdir(self, path):
        await self._enter()
        try:
            self.rmdirs.append(path)
        finally:
            await self._exit()


class _FakeAsyncRmPath:
    name = "root"

    def __init__(self, client, path="/root"):
        self.path = path
        self._sftpclient = SimpleNamespace(_aclient=client)

    def __truediv__(self, name):
        child = _FakeAsyncRmPath(self._sftpclient._aclient, f"{self.path}/{name}")
        child.name = name
        return child


def test_concurrent_rm_uses_native_asyncssh_calls_and_respects_max_concurrency():
    import asyncio

    tree = {
        "/root": ["a.txt", "b.txt", "c.txt", "d.txt"],
        "/root/a.txt": None,
        "/root/b.txt": None,
        "/root/c.txt": None,
        "/root/d.txt": None,
    }
    client = _FakeAsyncRmClient(tree, delay=0.01)

    asyncio.run(
        backend_mod._concurrent_rm(
            _FakeAsyncRmPath(client),
            max_concurrency=2,
            missing_ok=False,
            on_error=None,
        )
    )

    assert set(client.removed) == {"/root/a.txt", "/root/b.txt", "/root/c.txt", "/root/d.txt"}
    assert client.rmdirs == ["/root"]
    assert 1 < client.max_active <= 2


def test_concurrent_rm_ignore_error_receives_error_and_path():
    import asyncio

    tree = {
        "/root": ["ok.txt", "bad.txt"],
        "/root/ok.txt": None,
        "/root/bad.txt": None,
    }
    client = _FakeAsyncRmClient(tree, fail_remove={"/root/bad.txt"})
    ignored = []

    asyncio.run(
        backend_mod._concurrent_rm(
            _FakeAsyncRmPath(client),
            max_concurrency=4,
            missing_ok=False,
            on_error=lambda error, path: ignored.append((type(error), path.path)) or True,
        )
    )

    assert client.removed == ["/root/ok.txt"]
    assert ignored == [(OSError, "/root/bad.txt")]
    assert client.rmdirs == ["/root"]


def test_concurrent_rm_missing_root_honors_missing_ok():
    import asyncio

    client = _FakeAsyncRmClient({})

    asyncio.run(
        backend_mod._concurrent_rm(
            _FakeAsyncRmPath(client),
            max_concurrency=4,
            missing_ok=True,
            on_error=None,
        )
    )

    assert client.removed == []
    assert client.rmdirs == []


def test_sftppath_rm_recursive_uses_concurrent_helper(monkeypatch):
    import asyncio

    recorded = {}

    async def _fake_concurrent_rm(path, **kwargs):
        recorded["path"] = path
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(backend_mod, "_concurrent_rm", _fake_concurrent_rm)
    monkeypatch.setattr(backend_mod, "_run", lambda coro: asyncio.run(coro))

    src = sftp_pkg.SftpPath(
        "sftp://host/src", backend=backend_mod.AsyncsshSftpBackend(max_concurrency=6)
    )
    src.rm(recursive=True, missing_ok=True, ignore_error=True)

    assert recorded["path"] is src
    assert recorded["kwargs"]["max_concurrency"] == 6
    assert recorded["kwargs"]["missing_ok"] is True
    assert recorded["kwargs"]["on_error"](ValueError("ignored"), src) is True
