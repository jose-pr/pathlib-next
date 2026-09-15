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
    monkeypatch.delenv(
        sftp_pkg.__dict__.get("_ENV_VAR", "PATHLIB_NEXT_SFTP_BACKEND"), raising=False
    )
    yield


from pathlib_next.uri.schemes import sftp as sftp_pkg  # noqa: E402

import importlib.util as _importutil  # noqa: E402

_HAS_PARAMIKO = _importutil.find_spec("paramiko") is not None
_needs_paramiko = pytest.mark.skipif(
    not _HAS_PARAMIKO, reason="paramiko not installed (asyncssh-only install)"
)


def test_resolve_default_backend_auto_prefers_asyncssh(monkeypatch):
    monkeypatch.delenv(sftp_pkg._ENV_VAR, raising=False)
    cls = sftp_pkg._resolve_default_backend_cls(reload=True)
    assert cls is backend_mod.AsyncsshSftpBackend


@_needs_paramiko
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
    monkeypatch.delitem(sftp_pkg._BACKEND_REGISTRY, "asyncssh", raising=False)
    monkeypatch.setattr(sftp_pkg, "_asyncssh_probed", True)  # skip the real probe
    with pytest.raises(ImportError, match="sftp-async"):
        sftp_pkg._resolve_default_backend_cls(reload=True)


def test_resolve_default_backend_result_is_cached(monkeypatch):
    # asyncssh first (always available in this test module), then flip the env
    # and confirm no-reload returns the cached asyncssh result.
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "asyncssh")
    first = sftp_pkg._resolve_default_backend_cls(reload=True)
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "auto")
    second = sftp_pkg._resolve_default_backend_cls()  # no reload -- cached
    assert first is second is backend_mod.AsyncsshSftpBackend


@_needs_paramiko
def test_default_backend_cls_class_attribute_wins_over_env(monkeypatch):
    monkeypatch.setenv(sftp_pkg._ENV_VAR, "asyncssh")

    class _PinnedSftpPath(sftp_pkg.SftpPath):
        _default_backend_cls = sftp_pkg.SftpBackend
        __SCHEMES = ()  # avoid registering a second sftp: scheme

    inst = _PinnedSftpPath.__new__(_PinnedSftpPath)
    backend = inst._initbackend()
    assert isinstance(backend, sftp_pkg.SftpBackend)


def test_sftp_scheme_imports_and_resolves_without_paramiko():
    """Regression: an asyncssh-only install (no paramiko) must still import
    SftpPath and auto-resolve to the asyncssh backend.

    Runs in a subprocess with ``paramiko`` masked (blocked in sys.modules) so the
    guard holds even in a CI env where paramiko happens to be installed. Before
    the fix, ``uri/schemes/sftp/__init__`` imported ``._paramiko`` eagerly, so
    merely importing ``SftpPath`` raised ModuleNotFoundError without paramiko.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import sys
        # Make `import paramiko` fail, simulating an asyncssh-only install.
        sys.modules["paramiko"] = None
        from pathlib_next.uri.schemes.sftp import SftpPath
        from pathlib_next.uri.schemes import sftp as pkg
        sp = SftpPath("sftp://root@h:22/etc/hosts")
        assert sp.source.host == "h" and sp.path == "/etc/hosts"
        cls = pkg._resolve_default_backend_cls(reload=True)
        assert cls.__name__ == "AsyncsshSftpBackend", cls
        print("OK")
        """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


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


@_needs_paramiko
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


def test_asyncssh_backend_max_concurrency_defaults_to_16():
    # Raised from 8 to 16 in 0.8.3 (loopback recursive-copy sweep); see
    # DEFAULT_MAX_CONCURRENCY's rationale in _asyncssh.py.
    backend = backend_mod.AsyncsshSftpBackend()
    assert backend.max_concurrency == 16
    assert (
        backend.max_concurrency
        == backend_mod.AsyncsshSftpBackend.DEFAULT_MAX_CONCURRENCY
    )
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
    # Host-key verification stays on: nothing may inject known_hosts=None
    # (which disables asyncssh's check) unless the caller asked for it.
    assert "known_hosts" not in calls["kwargs"]
    assert calls["kwargs"]["username"] == "user"
    assert calls["kwargs"]["password"] == "pass"
    assert calls["sftp_version"] == 4


def test_aconnect_passes_explicit_known_hosts_opt_out_through(monkeypatch):
    import asyncio

    calls = {}

    class _FakeConn:
        async def start_sftp_client(self, *, sftp_version):
            return object()

    async def _fake_connect(host, port, **kwargs):
        calls["kwargs"] = kwargs
        return _FakeConn()

    monkeypatch.setattr(backend_mod._asyncssh, "connect", _fake_connect)
    backend = backend_mod.AsyncsshSftpBackend({"known_hosts": None})
    asyncio.run(
        backend_mod._aconnect(
            Source("sftp", None, "host", 22), connect_opts=backend.connect_opts
        )
    )
    assert "known_hosts" in calls["kwargs"]
    assert calls["kwargs"]["known_hosts"] is None


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

    run_timeouts = []

    def _fake_run(coro, timeout="unset"):
        run_timeouts.append(timeout)
        return asyncio.run(coro)

    aclient = object()
    monkeypatch.setattr(backend_mod, "_concurrent_copy", _fake_concurrent_copy)
    monkeypatch.setattr(backend_mod, "_run", _fake_run)
    monkeypatch.setattr(sftp_pkg.SftpPath, "is_dir", lambda self: True)
    monkeypatch.setattr(
        backend_mod.AsyncsshSftpBackend,
        "client",
        lambda self, source: SimpleNamespace(_aclient=aclient),
    )

    src = sftp_pkg.SftpPath(
        "sftp://host/src", backend=backend_mod.AsyncsshSftpBackend(max_concurrency=5)
    )
    target = sftp_pkg.SftpPath("sftp://host/dst", backend=src.backend)
    monkeypatch.setattr(type(target), "exists", lambda self: False)
    monkeypatch.setattr(
        type(target),
        "mkdir",
        lambda self, mode=0o777, parents=False, exist_ok=False: None,
    )

    src.copy(target, recursive=True, overwrite=True)

    assert recorded["path"] is src
    assert recorded["target"] is target
    assert recorded["kwargs"]["max_concurrency"] == 5
    # The client was resolved on the calling thread, not inside the coroutine.
    assert recorded["kwargs"]["aclient"] is aclient
    # A whole-tree operation carries no wall-clock bound.
    assert run_timeouts == [None]


# --- concurrent remove tests ---------------------------------------------


def _sftp_attrs(is_dir):
    permissions = 0o40755 if is_dir else 0o100644
    file_type = (
        asyncssh.FILEXFER_TYPE_DIRECTORY if is_dir else asyncssh.FILEXFER_TYPE_REGULAR
    )
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

    assert set(client.removed) == {
        "/root/a.txt",
        "/root/b.txt",
        "/root/c.txt",
        "/root/d.txt",
    }
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
            on_error=lambda error, path: ignored.append((type(error), path.path))
            or True,
        )
    )

    assert client.removed == ["/root/ok.txt"]
    assert ignored == [(OSError, "/root/bad.txt")]
    assert client.rmdirs == ["/root"]


def test_concurrent_rm_fail_fast_cancels_queued_children():
    import asyncio

    tree = {
        "/root": ["bad.txt", "a.txt", "b.txt", "c.txt"],
        "/root/bad.txt": None,
        "/root/a.txt": None,
        "/root/b.txt": None,
        "/root/c.txt": None,
    }
    client = _FakeAsyncRmClient(
        tree,
        delay=0.01,
        fail_remove={"/root/bad.txt"},
    )

    with pytest.raises(OSError):
        asyncio.run(
            backend_mod._concurrent_rm(
                _FakeAsyncRmPath(client),
                max_concurrency=1,
                missing_ok=False,
                on_error=None,
            )
        )

    assert len(client.removed) < 3
    assert client.rmdirs == []


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

    run_timeouts = []

    def _fake_run(coro, timeout="unset"):
        run_timeouts.append(timeout)
        return asyncio.run(coro)

    aclient = object()
    monkeypatch.setattr(backend_mod, "_concurrent_rm", _fake_concurrent_rm)
    monkeypatch.setattr(backend_mod, "_run", _fake_run)
    monkeypatch.setattr(
        backend_mod.AsyncsshSftpBackend,
        "client",
        lambda self, source: SimpleNamespace(_aclient=aclient),
    )

    src = sftp_pkg.SftpPath(
        "sftp://host/src", backend=backend_mod.AsyncsshSftpBackend(max_concurrency=6)
    )
    src.rm(recursive=True, missing_ok=True, ignore_error=True)

    assert recorded["path"] is src
    assert recorded["kwargs"]["max_concurrency"] == 6
    assert recorded["kwargs"]["missing_ok"] is True
    assert recorded["kwargs"]["on_error"](ValueError("ignored"), src) is True
    assert recorded["kwargs"]["aclient"] is aclient
    assert run_timeouts == [None]


# --- default max_concurrency -------------------------------------------------


def test_explicit_max_concurrency_overrides_default():
    backend = backend_mod.AsyncsshSftpBackend({"config": None}, max_concurrency=4)
    assert backend.max_concurrency == 4
    assert (
        backend_mod.AsyncsshSftpBackend(
            {"config": None}, max_concurrency=1
        ).max_concurrency
        == 1
    )
    # 0 is honored as given (the async helpers clamp to >=1 at use time via
    # `max(1, max_concurrency)`), not silently replaced by the default.
    assert (
        backend_mod.AsyncsshSftpBackend(
            {"config": None}, max_concurrency=0
        ).max_concurrency
        == 0
    )


# --- concurrent recursive copy against a real server --------------------------
# The fakes above hold one flat directory. These run SftpPath.copy's native
# fan-out (`_concurrent_copy`) over conftest's in-process asyncssh server,
# whose root is `fixture_tree`, and read the result back from local disk.


@pytest.fixture
def asyncssh_tree(sftp_server, fixture_tree):
    from pathlib_next.uri.schemes.sftp import SftpPath

    backend = backend_mod.AsyncsshSftpBackend()
    root = SftpPath(sftp_server, backend=backend)
    yield root, fixture_tree
    backend_mod._CACHE.invalidate((backend, root.source))


def _bounded(call, timeout=60):
    """Run `call` on a worker thread: a fan-out deadlock fails the test
    instead of hanging the run (sync calls made on the asyncssh bridge loop can deadlock).
    """
    import threading

    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as error:  # re-raised on the test thread
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"copy did not finish within {timeout}s"
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _local_tree(root):
    return {
        p.relative_to(root).as_posix(): (None if p.is_dir() else p.read_bytes())
        for p in root.rglob("*")
    }


def test_concurrent_copy_nested_tree_and_overwrite_real_server(asyncssh_tree):
    root, local = asyncssh_tree
    (local / "sub" / "nested" / "deeper").mkdir()
    (local / "sub" / "nested" / "deeper" / "e.bin").write_bytes(b"\x00e" * 3000)

    _bounded(lambda: (root / "sub").copy(root / "copy", recursive=True))
    assert _local_tree(local / "copy") == _local_tree(local / "sub")

    # overwrite=True descends into the existing tree: an existing nested
    # directory is reused and an existing file replaced.
    (local / "sub" / "nested" / "d.py").write_bytes(b"changed")
    (local / "copy" / "stale.txt").write_bytes(b"kept")
    _bounded(lambda: (root / "sub").copy(root / "copy", recursive=True, overwrite=True))
    assert (local / "copy" / "nested" / "d.py").read_bytes() == b"changed"
    assert (local / "copy" / "nested" / "deeper" / "e.bin").read_bytes() == (
        b"\x00e" * 3000
    )
    assert (local / "copy" / "stale.txt").read_bytes() == b"kept"


def test_concurrent_copy_type_conflicts_in_existing_tree_real_server(asyncssh_tree):
    root, local = asyncssh_tree
    target = local / "conflict"
    target.mkdir()
    (target / "nested").write_bytes(b"a file where src has a directory")
    (target / "c.py").mkdir()  # a directory where src has a file

    errors = []
    _bounded(
        lambda: (root / "sub").copy(
            root / "conflict",
            recursive=True,
            overwrite=True,
            ignore_error=errors.append,
        )
    )
    assert sorted(type(error).__name__ for error in errors) == [
        "FileExistsError",
        "IsADirectoryError",
    ]
    # Neither conflicting entry was replaced.
    assert (target / "nested").read_bytes() == b"a file where src has a directory"
    assert (target / "c.py").is_dir()

    with pytest.raises((FileExistsError, IsADirectoryError)):
        _bounded(
            lambda: (root / "sub").copy(
                root / "conflict", recursive=True, overwrite=True
            )
        )


def test_concurrent_copy_symlink_child_uses_sync_fallback_real_server(asyncssh_tree):
    import os

    root, local = asyncssh_tree
    real = local / "sub" / "c.py"
    try:
        # Absolute: asyncssh's chrooted SFTPServer cannot map a relative link
        # target back into the chroot (readlink raises "File not found").
        os.symlink(str(real), local / "sub" / "link.py")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")
    try:
        if (root / "sub" / "link.py").readlink().path != "/sub/c.py":
            raise OSError("unexpected link target")
    except OSError as error:
        pytest.skip(f"this server does not serve symlinks: {error}")

    _bounded(
        lambda: (root / "sub").copy(
            root / "linked_copy", recursive=True, follow_symlinks=False
        )
    )
    copied = local / "linked_copy" / "link.py"
    assert copied.is_symlink()
    # The same target, compared as the server reports it: a local readlink
    # on Windows adds an extended-length path prefix.
    assert (root / "linked_copy" / "link.py").readlink().path == "/sub/c.py"
    assert copied.read_bytes() == b"c"
    assert (local / "linked_copy" / "nested" / "d.py").read_bytes() == b"d"


# --- sftp-translate-drops-errno-filename -----------------------------------------


@pytest.mark.parametrize(
    "error, expected_type, code",
    [
        (asyncssh.SFTPNoSuchFile("No such file"), FileNotFoundError, "ENOENT"),
        (asyncssh.SFTPNoSuchPath("No such path"), FileNotFoundError, "ENOENT"),
        (asyncssh.SFTPFileAlreadyExists("exists"), FileExistsError, "EEXIST"),
        (asyncssh.SFTPPermissionDenied("denied"), PermissionError, "EACCES"),
        (asyncssh.SFTPDirNotEmpty("not empty"), OSError, "ENOTEMPTY"),
    ],
)
def test_translate_sets_errno_and_filename(error, expected_type, code):
    import errno

    result = backend_mod._translate(error, "/srv/a.txt")
    assert type(result) is expected_type
    assert result.errno == getattr(errno, code)
    assert result.filename == "/srv/a.txt"
    assert result.strerror == str(error)


def test_translate_bare_failure_keeps_errno_unset_but_names_the_path():
    # SFTPv3 has no ENOTEMPTY/EISDIR status: `SftpPath` reads a missing
    # errno as "consult the entry", exactly as on the paramiko backend.
    result = backend_mod._translate(asyncssh.SFTPFailure("failure"), "/srv/d")
    assert type(result) is OSError
    assert result.errno is None
    assert result.filename == "/srv/d"
    assert result.strerror == "failure"
    assert "/srv/d" in str(result)
    assert backend_mod._translate(asyncssh.SFTPFailure("failure")).errno is None


def test_client_method_errors_name_their_paths():
    class _AClient:
        async def stat(self, path):
            raise asyncssh.SFTPNoSuchFile("No such file")

        async def rename(self, old, new):
            raise asyncssh.SFTPPermissionDenied("denied")

    import errno

    client = backend_mod._SyncSftpClient(_AClient())
    with pytest.raises(FileNotFoundError) as missing:
        client.stat("/srv/missing.txt")
    assert missing.value.errno == errno.ENOENT
    assert missing.value.filename == "/srv/missing.txt"
    with pytest.raises(PermissionError) as denied:
        client.rename("/srv/a", "/srv/b")
    assert (denied.value.filename, denied.value.filename2) == ("/srv/a", "/srv/b")


def test_asyncssh_backend_errors_carry_errno_and_filename(sftp_server):
    import errno

    from pathlib_next.uri.schemes.sftp import SftpPath
    from pathlib_next.uri.schemes.sftp._asyncssh import AsyncsshSftpBackend

    backend = AsyncsshSftpBackend()
    root = SftpPath(sftp_server, backend=backend)
    try:
        with pytest.raises(FileNotFoundError) as missing:
            (root / "missing.txt").read_bytes()
        assert missing.value.errno == errno.ENOENT
        assert missing.value.filename == (root / "missing.txt").path
        with pytest.raises(FileExistsError) as exists:
            (root / "sub").mkdir()
        assert exists.value.errno == errno.EEXIST
        assert exists.value.filename == str(root / "sub")
    finally:
        backend_mod._CACHE.invalidate((backend, root.source))
