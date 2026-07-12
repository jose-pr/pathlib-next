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

    @_utils.notimplemented
    def client(self, source: Source): ...


from ._paramiko import SftpBackend as SftpBackend  # noqa: E402


# --- backend selection -----------------------------------------------------
# Precedence, highest to lowest (each layer only consulted if the one above
# doesn't apply): explicit `backend=` kwarg on construction (already how
# UriPath backend propagation works, unchanged) > `SftpPath._default_backend_cls`
# class attribute > `PATHLIB_NEXT_SFTP_BACKEND` env var > auto-detect
# (asyncssh if importable, else paramiko).

_ENV_VAR = "PATHLIB_NEXT_SFTP_BACKEND"
_BACKEND_REGISTRY: "dict[str, type[BaseSftpBackend]]" = {"paramiko": SftpBackend}
_asyncssh_probed = False
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


def _resolve_default_backend_cls(reload: bool = False) -> "type[BaseSftpBackend]":
    global _resolved_backend_cls
    if not reload and _resolved_backend_cls is not None:
        return _resolved_backend_cls
    value = _os.environ.get(_ENV_VAR, "auto")
    if value == "paramiko":
        cls = _BACKEND_REGISTRY["paramiko"]
    else:
        _probe_asyncssh()
        if value == "auto":
            cls = _BACKEND_REGISTRY.get("asyncssh") or _BACKEND_REGISTRY["paramiko"]
        elif value == "asyncssh":
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
        else:
            raise ValueError(
                f"{_ENV_VAR}={value!r} is not a recognized SFTP backend "
                f"(expected one of {sorted({'auto', *_BACKEND_REGISTRY})!r})"
            )
    _resolved_backend_cls = cls
    return cls


def __getattr__(name: str):
    # PEP 562 lazy module attribute: `from .sftp import AsyncsshSftpBackend`
    # (or `sftp.AsyncsshSftpBackend`) only imports asyncssh at the point
    # it's actually referenced -- importing `pathlib_next.uri.schemes.sftp`
    # itself (which happens for every `sftp:` URL, regardless of which
    # backend ends up selected) must not eagerly import asyncssh.
    if name == "AsyncsshSftpBackend":
        from ._asyncssh import AsyncsshSftpBackend

        return AsyncsshSftpBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class SftpPath(UriPath):
    """`sftp:` scheme: full read/write access, auto-selecting between a
    paramiko (sync) and an asyncssh (async, bridged) backend -- see
    "backend selection" above. Requires the `sftp` extra (paramiko) or
    `sftp-async` extra (asyncssh)."""

    __SCHEMES = ("sftp",)
    __slots__ = ()

    #: Class-level backend override, for a subclass to pin its own default
    #: without touching process env state. Wins over the env var, loses to
    #: an explicit `backend=` constructor kwarg.
    _default_backend_cls: "type[BaseSftpBackend] | None" = None

    if _ty.TYPE_CHECKING:
        backend: BaseSftpBackend

    def _initbackend(self):
        cls = self._default_backend_cls or _resolve_default_backend_cls()
        return cls.default()

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

    def symlink_to(self, target: "SftpPath | Uri | str", target_is_directory=False):
        # target_is_directory is a Windows-local-filesystem-only hint
        # (pathlib.Path.symlink_to() signature parity) -- accepted and
        # ignored, same as every other non-local scheme. Core SFTPv3
        # operation on both backends, no capability gate needed. Both
        # libraries' symlink() already auto-correct for OpenSSH's
        # well-known swapped wire argument order internally.
        target_path = target.path if isinstance(target, Uri) else str(target)
        self._sftpclient.symlink(target_path, self.path)

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
            raise NotImplementedError(
                "hardlink_to() requires the asyncssh backend"
            )
        target_path = target.path if isinstance(target, Uri) else str(target)
        self._sftpclient.link(target_path, self.path)

    def copy(
        self,
        target,
        *,
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=True,
        recursive=False,
        ignore_error=None,
    ):
        """Copy with concurrent fan-out on the asyncssh backend.

        When using the asyncssh backend with `recursive=True` on a
        directory, child copies are fanned out over worker threads,
        bounded by `backend.max_concurrency`.
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
