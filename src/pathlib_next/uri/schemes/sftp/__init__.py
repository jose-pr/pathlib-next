from __future__ import annotations

import os as _os
import typing as _ty

from .... import utils as _utils
from ....utils.stat import FileStat
from ... import Source, Uri, UriPath


class BaseSftpBackend(object):
    """Protocol for obtaining a paramiko-shaped `SFTPClient` for a
    `Source`. Subclass this to plug in custom connection handling (e.g.
    tests mock it directly, no real server); `SftpBackend` (paramiko,
    `_paramiko.py`) and `AsyncsshSftpBackend` (`_asyncssh.py`, optional
    extra) are the real implementations. Connection caching is each
    backend's own responsibility -- `client()` is expected to return an
    already-cached-or-freshly-opened, ready-to-use client; `SftpPath`
    itself does no per-backend branching anywhere."""

    __slots__ = ()

    #: Whether `chmod(follow_symlinks=False)` is supported. paramiko has no
    #: lchmod equivalent to call; asyncssh's `chmod()` takes
    #: `follow_symlinks` natively.
    supports_lchmod = False
    #: Whether `hardlink_to()` is supported. SFTPv3 (paramiko's ceiling)
    #: has no core hard-link operation at all.
    supports_hardlink = False

    def supported_checksums(self, path: "SftpPath") -> "_ty.FrozenSet[str]":
        """Advisory set of algorithm names `checksum()` can currently
        produce against `path`'s server connection (see
        `protocols.checksum.NativeChecksum.supported_checksums`). Empty
        here (the default): no client-library support for any
        native-hashing extension at all -- true for `AsyncsshSftpBackend`,
        which inherits this. `SftpBackend` (paramiko) overrides this with a
        real per-connection probe against `path`, since neither paramiko
        nor asyncssh expose the server's advertised SFTP extension list
        (paramiko's version-negotiation code reads and discards it -- see
        `_paramiko.py::SftpBackend.supported_checksums`). Takes a `path`
        argument (unlike a bare capability flag) because the only reliable
        way to know is to actually try the extension against a real file.
        """
        return frozenset()

    @_utils.notimplemented
    def client(self, source: Source): ...

    @_utils.notimplemented
    def checksum(self, path: "SftpPath", algorithm: str) -> str:
        """Backend-native digest for `path`'s content, e.g. via the
        OpenSSH `check-file@openssh.com` SFTP protocol extension. Raises
        `NotImplementedError` (the base/default here) when the backend has
        no such capability at all, and MUST also raise it -- not return a
        value -- when the server doesn't advertise `algorithm` specifically
        (see `protocols/checksum.py::NativeChecksum.checksum` for why this
        is a hard contract, not a style choice). Only `SftpBackend`
        (paramiko) implements this today; `AsyncsshSftpBackend` has no
        equivalent client-library support to build it on (see
        `_asyncssh.py`), so it inherits this default and always falls back
        to streaming.
        """
        ...


# The default-config sentinel is paramiko-free (lives in `_sshconfig`) so
# importing this scheme never pulls paramiko in just to have the sentinel.
from ._sshconfig import _DEFAULT_SSH_CONFIG

# --- backend selection -----------------------------------------------------
# Precedence, highest to lowest (each layer only consulted if the one above
# doesn't apply): explicit `backend=` kwarg on construction (already how
# UriPath backend propagation works, unchanged) > `SftpPath._default_backend_cls`
# class attribute > `PATHLIB_NEXT_SFTP_BACKEND` env var > auto-detect
# (asyncssh if importable, else paramiko).
#
# BOTH backends are imported lazily (`_probe_asyncssh`/`_probe_paramiko`): merely
# importing this scheme -- which happens for every `sftp:` URL and for
# `from ...sftp import SftpPath` -- must not require *either* SSH library. In
# particular an asyncssh-only install (the `sftp-async` extra, no paramiko) must
# be able to import and use `SftpPath`; eagerly importing `._paramiko` here broke
# exactly that.

_ENV_VAR = "PATHLIB_NEXT_SFTP_BACKEND"
_BACKEND_REGISTRY: "dict[str, type[BaseSftpBackend]]" = {}
_asyncssh_probed = False
_paramiko_probed = False
_resolved_backend_cls: "type[BaseSftpBackend] | None" = None


def _probe_asyncssh() -> None:
    # Lazy and only-once: a caller that forces PATHLIB_NEXT_SFTP_BACKEND=
    # paramiko (or never triggers backend resolution at all) never imports
    # asyncssh -- scheme loading already avoids paying for heavy unused
    # imports elsewhere (entry-point plugin discovery), this preserves that.
    global _asyncssh_probed
    if _asyncssh_probed:
        return
    _asyncssh_probed = True
    try:
        from ._asyncssh import AsyncsshSftpBackend
    except ImportError:
        return
    _BACKEND_REGISTRY["asyncssh"] = AsyncsshSftpBackend


def _probe_paramiko() -> None:
    # Symmetric with `_probe_asyncssh`: only import paramiko when it is actually
    # needed (paramiko selected, or auto-detect with asyncssh unavailable), so an
    # asyncssh-only install never imports paramiko.
    global _paramiko_probed
    if _paramiko_probed:
        return
    _paramiko_probed = True
    try:
        from ._paramiko import SftpBackend
    except ImportError:
        return
    _BACKEND_REGISTRY["paramiko"] = SftpBackend


def _resolve_default_backend_cls(reload: bool = False) -> "type[BaseSftpBackend]":
    global _resolved_backend_cls
    if not reload and _resolved_backend_cls is not None:
        return _resolved_backend_cls
    value = _os.environ.get(_ENV_VAR, "auto")
    if value == "paramiko":
        _probe_paramiko()
        if "paramiko" not in _BACKEND_REGISTRY:
            raise ImportError(
                f"{_ENV_VAR}=paramiko but the paramiko package is not "
                "installed -- install the 'sftp' extra, or unset "
                f"{_ENV_VAR} to auto-detect (uses asyncssh if available)."
            )
        cls = _BACKEND_REGISTRY["paramiko"]
    elif value == "asyncssh":
        _probe_asyncssh()
        if "asyncssh" not in _BACKEND_REGISTRY:
            # Fail loud -- a silent fallback to paramiko would hide a
            # deployment misconfiguration (asyncssh extra not installed
            # where the operator explicitly asked for it).
            raise ImportError(
                f"{_ENV_VAR}=asyncssh but the asyncssh package is not "
                "installed -- install the 'sftp-async' extra, or unset "
                f"{_ENV_VAR} to auto-detect (falls back to paramiko)."
            )
        cls = _BACKEND_REGISTRY["asyncssh"]
    elif value == "auto":
        _probe_asyncssh()
        if "asyncssh" not in _BACKEND_REGISTRY:
            _probe_paramiko()
        cls = _BACKEND_REGISTRY.get("asyncssh") or _BACKEND_REGISTRY.get("paramiko")
        if cls is None:
            raise ImportError(
                "no SFTP backend available -- install the 'sftp-async' "
                "(asyncssh) or 'sftp' (paramiko) extra."
            )
    else:
        raise ValueError(
            f"{_ENV_VAR}={value!r} is not a recognized SFTP backend "
            "(expected one of 'auto', 'asyncssh', 'paramiko')"
        )
    _resolved_backend_cls = cls
    return cls


def __getattr__(name: str):
    # PEP 562 lazy module attributes: referencing `AsyncsshSftpBackend`,
    # `SftpBackend` (paramiko), or `_DEFAULT_SSH_CONFIG` via
    # `from .sftp import ...` imports the relevant backend only at that point --
    # importing the scheme module itself pulls in neither SSH library.
    if name == "AsyncsshSftpBackend":
        from ._asyncssh import AsyncsshSftpBackend

        return AsyncsshSftpBackend
    if name == "SftpBackend":
        from ._paramiko import SftpBackend

        return SftpBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class SftpPath(UriPath):
    """`sftp:` scheme: full read/write access, auto-selecting between a
    paramiko (sync) and an asyncssh (async, bridged) backend -- see
    "backend selection" above. Requires the `sftp` extra (paramiko) or
    `sftp-async` extra (asyncssh). Also implements
    `protocols.checksum.NativeChecksum` (`checksum()`, delegating to
    `self.backend.checksum()`) -- native on the paramiko backend via the
    OpenSSH `check-file@openssh.com` extension, `NotImplementedError`
    (falls back to streaming) on asyncssh or a server without that
    extension."""

    __SCHEMES = ("sftp",)
    __slots__ = ("_ssh_config",)
    _host_filesystem_path = True

    #: Class-level backend override, for a subclass to pin its own default
    #: without touching process env state. Wins over the env var, loses to
    #: an explicit `backend=` constructor kwarg.
    _default_backend_cls: "type[BaseSftpBackend] | None" = None

    if _ty.TYPE_CHECKING:
        backend: BaseSftpBackend

    def _initbackend(self):
        cls = self._default_backend_cls or _resolve_default_backend_cls()
        return cls.default(ssh_config=self._ssh_config)

    def _init(
        self,
        source,
        path,
        query,
        fragment,
        /,
        backend=None,
        ssh_config=_DEFAULT_SSH_CONFIG,
        **kwargs,
    ):
        self._ssh_config = ssh_config
        return super()._init(
            source,
            path,
            query,
            fragment,
            backend=backend,
            **kwargs,
        )

    @property
    def _sftpclient(self):
        return self.backend.client(self.source)

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def _scandir(self):
        # listdir_attr() gets attrs (lstat-like -- symlinks are not
        # resolved) for every child in one round trip, instead of a plain
        # name list (listdir()) plus a separate stat()/lstat() per child.
        for attr in self._sftpclient.listdir_attr(self.path):
            yield attr.filename, FileStat.from_stat(attr)

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None and not follow_symlinks:
            # The hint comes from listdir_attr(), which never resolves
            # symlinks -- only safe to reuse for a follow_symlinks=False
            # (lstat-equivalent) request.
            return hint
        if follow_symlinks:
            return self._sftpclient.stat(self.path)
        else:
            return self._sftpclient.lstat(self.path)

    def _open(self, mode="r", buffering=-1):
        try:
            return self._sftpclient.open(self.path, mode, buffering)
        except OSError as error:
            # SFTPv3 has no dedicated "already exists" status code -- an
            # O_EXCL ("x" mode) failure comes back as a generic failure,
            # not the ENOENT-mapped FileNotFoundError already raised
            # correctly for a genuinely missing file/parent. True on both
            # backends against a real-world (v3) server.
            if "x" in mode and self.exists():
                raise FileExistsError(self) from error
            raise

    def _mkdir(self, mode):
        try:
            return self._sftpclient.mkdir(self.path, mode)
        except OSError as error:
            # Same SFTPv3 status-code gap as _open() above: mkdir on an
            # existing path also comes back as a generic failure.
            if self.exists():
                raise FileExistsError(self) from error
            raise

    def chmod(self, mode, *, follow_symlinks=True):
        if follow_symlinks:
            return self._sftpclient.chmod(self.path, mode)
        if not self.backend.supports_lchmod:
            raise NotImplementedError("chmod(follow_symlinks=False)")
        return self._sftpclient.chmod(self.path, mode, follow_symlinks=False)

    def supported_checksums(self) -> "_ty.FrozenSet[str]":
        """`protocols.checksum.NativeChecksum` implementation: delegates to
        `self.backend.supported_checksums(self)`. Empty on the asyncssh
        backend (no client-library support); a real per-connection probe
        on the paramiko backend (see
        `_paramiko.py::SftpBackend.supported_checksums`), so this can be
        empty even on the paramiko backend if the connected server doesn't
        actually implement `check-file@openssh.com`.
        """
        return self.backend.supported_checksums(self)

    def checksum(self, algorithm: str = "md5") -> str:
        """`protocols.checksum.NativeChecksum` implementation: delegates to
        `self.backend.checksum()` (the OpenSSH `check-file@openssh.com`
        SFTP extension on the paramiko backend; unimplemented on asyncssh
        -- see `BaseSftpBackend.checksum`). Any failure that isn't already
        `NotImplementedError` (a server that doesn't advertise the
        extension, an unsupported algorithm, a transport-level error) is
        also translated to `NotImplementedError`: this method's whole
        contract is "raise if a genuine digest can't be produced", and a
        caller (e.g. `PathSyncer`) must be able to fall back to streaming
        on ANY such failure, not just the backend's own explicit signal.
        """
        try:
            return self.backend.checksum(self, algorithm)
        except NotImplementedError:
            raise
        except Exception as error:
            raise NotImplementedError(
                f"native checksum unavailable: {error}"
            ) from error

    def unlink(self, missing_ok=False):
        if missing_ok and not self.exists():
            return
        return self._sftpclient.remove(self.path)

    def rmdir(self):
        return self._sftpclient.rmdir(self.path)

    def rename(self, target: "SftpPath | Uri | str"):
        # base Path.rename is the notimplemented stub -- this was never
        # called under its old name `_rename`, so every move() fell back to
        # copy+unlink. `target.path`, not as_posix(): Uri.as_posix() prefixes
        # "host:" for the sftp wire protocol, which only wants the raw path.
        # A plain str target is resolved relative to self's *parent*
        # (sibling rename -- "rename this file to a new name in the same
        # directory"), not to self itself (which would join it as a child).
        if not isinstance(target, Uri):
            target = Uri(self.parent, target)
        return self._sftpclient.rename(self.path, target.path)

    def _symlink_to(self, target: "SftpPath | Uri", target_is_directory=False):
        # The backend primitive only -- `Path.symlink_to()` owns the
        # str->path normalization and the `force=` unlink-then-symlink
        # sequence, so this stays one wire call.
        #
        # `.path`, not as_posix(): Uri.as_posix() prefixes "host:" for the
        # sftp wire protocol, which only wants the raw path -- same reason
        # rename() above uses it, and what keeps a relative target from
        # becoming "host:real.txt".
        #
        # target_is_directory is a Windows-local-filesystem-only hint
        # (pathlib.Path.symlink_to() signature parity) -- accepted and
        # ignored, same as every other non-local scheme. Core SFTPv3
        # operation on both backends, no capability gate needed. Both
        # libraries' symlink() already auto-correct for OpenSSH's
        # well-known swapped wire argument order internally.
        self._sftpclient.symlink(target.path, self.path)

    def readlink(self) -> "SftpPath":
        # Returns the raw target string, unresolved -- relative targets
        # stay relative (mirrors pathlib.Path.readlink()'s
        # `self.with_segments(os.readlink(self))`). Do NOT resolve against
        # self.parent: unlike rename()'s destination argument, this is a
        # *result*, and resolving it would silently diverge from pathlib
        # on the one method whose entire job is reporting the stored
        # target as-is.
        target = self._sftpclient.readlink(self.path)
        return self.with_segments(target)

    def hardlink_to(self, target: "SftpPath | Uri | str"):
        if not self.backend.supports_hardlink:
            raise NotImplementedError("hardlink_to() requires the asyncssh backend")
        target_path = target.path if isinstance(target, Uri) else str(target)
        self._sftpclient.link(target_path, self.path)

    def rm(
        self,
        /,
        recursive=False,
        missing_ok=False,
        ignore_error: bool | _ty.Callable[[Exception, _ty.Self], bool] = False,
    ):
        try:
            from ._asyncssh import AsyncsshSftpBackend, _concurrent_rm, _run
        except ImportError:
            return super().rm(
                recursive=recursive,
                missing_ok=missing_ok,
                ignore_error=ignore_error,
            )

        if not isinstance(self.backend, AsyncsshSftpBackend) or not recursive:
            return super().rm(
                recursive=recursive,
                missing_ok=missing_ok,
                ignore_error=ignore_error,
            )

        on_error = None
        if ignore_error:
            on_error = (
                ignore_error
                if callable(ignore_error)
                else lambda _err, _path: bool(ignore_error)
            )

        return _run(
            _concurrent_rm(
                self,
                max_concurrency=self.backend.max_concurrency,
                missing_ok=missing_ok,
                on_error=on_error,
            )
        )

    def copy(
        self,
        target,
        *,
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=True,
        recursive=False,
        ignore_error=None,
        progress=None,
    ):
        """Copy with concurrent fan-out on the asyncssh backend.

        When using the asyncssh backend with `recursive=True` on a
        directory, child copies are fanned out over worker threads,
        bounded by `backend.max_concurrency`. `progress` is honored on the
        generic single-file fallback path below, but **not** called during
        the concurrent native fan-out itself -- see `docs/divergences.md`'s
        "Deliberate extensions" section for the documented limitation.
        """
        from ._asyncssh import AsyncsshSftpBackend, _concurrent_copy, _run

        if (
            not isinstance(self.backend, AsyncsshSftpBackend)
            or not recursive
            or not self.is_dir()
        ):
            return super().copy(
                target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                recursive=recursive,
                ignore_error=ignore_error,
                progress=progress,
            )

        if isinstance(target, str):
            target = type(self)(target)

        if target.exists():
            if not target.is_dir():
                raise FileExistsError(target)
            if not overwrite:
                raise FileExistsError(target)
        else:
            target.mkdir()

        coro = _concurrent_copy(
            self,
            target,
            overwrite=overwrite,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
            max_concurrency=self.backend.max_concurrency,
            ignore_error=ignore_error,
        )
        return _run(coro)
