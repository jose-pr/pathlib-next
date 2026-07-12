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
