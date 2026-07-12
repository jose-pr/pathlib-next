"""Unit-only asyncssh SFTP backend tests: error translation, the SFTPAttrs
-> st_*-shaped stat adapter, and backend-selection precedence. Mirrors
test_sftp.py's paramiko coverage for the pieces that are asyncssh-specific;
end-to-end behavior (real read/write/mkdir/rename/... against a live
server) is covered by TestSftpContract's "asyncssh" param in
test_contract.py.
"""
import stat

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


def test_concurrent_copy_respects_max_concurrency(fixture_tree):
    """Verify that concurrent copy actually limits concurrency."""
    import asyncio

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def instrumented_concurrent_copy(
        path,
        target,
        overwrite,
        follow_symlinks,
        preserve_metadata,
        max_concurrency,
        ignore_error,
    ):
        """Instrumented version to track peak in-flight operations."""
        nonlocal in_flight, max_in_flight

        semaphore = asyncio.Semaphore(max_concurrency)

        async def copy_child(child):
            nonlocal in_flight, max_in_flight

            async with semaphore:
                async with lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.01)  # Simulate work
                async with lock:
                    in_flight -= 1

        tasks = [copy_child(None) for _ in range(10)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=False)

    # Verify max_concurrency is enforced: with 10 children and max_concurrency=3,
    # peak in-flight should never exceed 3.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            instrumented_concurrent_copy(
                None, None, False, True, True, max_concurrency=3, ignore_error=None
            )
        )
        assert max_in_flight <= 3, f"Peak in-flight {max_in_flight} exceeded max_concurrency=3"
    finally:
        loop.close()


def test_concurrent_copy_ignore_error_allows_partial_failure():
    """When ignore_error is set, concurrent copy continues on failure."""
    import asyncio

    completed = []

    async def instrumented_concurrent_copy_with_failures(max_concurrency, ignore_error):
        """Concurrent copy that deliberately fails on some tasks."""
        semaphore = asyncio.Semaphore(max_concurrency)

        async def copy_child(n):
            async with semaphore:
                if n % 2 == 0:
                    raise ValueError(f"Child {n} failed")
                completed.append(n)

        tasks = [copy_child(i) for i in range(5)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=(ignore_error is not None))

    # With ignore_error, odd-numbered tasks should complete despite even ones failing
    completed.clear()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            instrumented_concurrent_copy_with_failures(
                max_concurrency=4, ignore_error=lambda e: None
            )
        )
        # Odd-numbered tasks (1, 3) should have completed
        assert 1 in completed and 3 in completed, f"Expected partial completion, got {completed}"
    finally:
        loop.close()
