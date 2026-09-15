from __future__ import annotations

import errno as _errno
import os as _os
import typing as _ty
import weakref as _weakref

from .... import utils as _utils
from ....path import _contains
from ....utils.stat import FileStat
from ... import _NOSOURCE, Source, Uri, UriPath

#: SFTP clients on which `posix-rename@openssh.com` proved unavailable (the
#: extension request failed and a plain rename then succeeded), so later
#: renames skip the doomed request. Weak: a reconnect starts over.
_NO_POSIX_RENAME: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()


def _posix_rename_known_unsupported(client) -> bool:
    try:
        return client in _NO_POSIX_RENAME
    except TypeError:
        return False


def _mark_posix_rename_unsupported(client) -> None:
    try:
        _NO_POSIX_RENAME[client] = True
    except TypeError:
        pass


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

    def _wire_open_mode(self, mode: str) -> str:
        """The mode string this backend's client `open()` needs for a
        pathlib `_open()` mode (no "b"). Default: unchanged."""
        return mode

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
        per-connection probe against `path` (paramiko's version negotiation
        reads and discards the server's extension list -- see
        `_paramiko.py::SftpBackend.supported_checksums`). Takes a `path`
        argument (unlike a bare capability flag) because the only reliable
        way to know is to actually try the extension against a real file.
        """
        return frozenset()

    @_utils.notimplemented
    def client(self, source: Source): ...

    def close(self) -> None:
        """Close every connection this backend has cached. A no-op here;
        both real backends override it. The backend stays usable: the next
        `client()` call reconnects."""

    @_utils.notimplemented
    def checksum(self, path: "SftpPath", algorithm: str) -> str:
        """Backend-native digest for `path`'s content, e.g. via the
        filexfer draft's `check-file-handle` SFTP extension (implemented by
        some servers, e.g. ProFTPD's mod_sftp; OpenSSH has no such
        extension). Raises
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

# "No ssh_config argument given" -- distinct from _DEFAULT_SSH_CONFIG so a
# later lazy `_init()` keeps a value captured at construction.
_UNSET_SSH_CONFIG = object()

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
    filexfer draft's `check-file-handle` extension where the server
    implements it (OpenSSH does not), `NotImplementedError` (falls back to
    streaming) on asyncssh or a server without that extension."""

    __SCHEMES = ("sftp",)
    __slots__ = ("_ssh_config",)
    _host_filesystem_path = True

    #: Class-level backend override, for a subclass to pin its own default
    #: without touching process env state. Wins over the env var, loses to
    #: an explicit `backend=` constructor kwarg.
    _default_backend_cls: "type[BaseSftpBackend] | None" = None

    if _ty.TYPE_CHECKING:
        backend: BaseSftpBackend

    def __new__(cls, *args, ssh_config=_UNSET_SSH_CONFIG, **kwargs):
        # A direct `SftpPath(url, ssh_config=...)` parses lazily and never
        # passes its keywords to `_init()`, so capture the value here.
        inst = super().__new__(cls, *args, **kwargs)
        if isinstance(inst, SftpPath):
            if ssh_config is _UNSET_SSH_CONFIG:
                # Inherit from a path segment, the way the backend is.
                ssh_config = _DEFAULT_SSH_CONFIG
                for segment in reversed(args):
                    if isinstance(segment, SftpPath):
                        ssh_config = segment._ssh_config
                        break
            inst._ssh_config = ssh_config
        return inst

    def _initbackend(self):
        cls = self._default_backend_cls or _resolve_default_backend_cls()
        return cls.default(ssh_config=self._ssh_config)

    def _from_parsed_parts(self, source, path, query, fragment, /, **kwargs):
        kwargs.setdefault("ssh_config", self._ssh_config)
        return super()._from_parsed_parts(source, path, query, fragment, **kwargs)

    def _init(
        self,
        source,
        path,
        query,
        fragment,
        /,
        backend=None,
        ssh_config=_UNSET_SSH_CONFIG,
        **kwargs,
    ):
        if ssh_config is not _UNSET_SSH_CONFIG:
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
        try:
            attrs = self._sftpclient.listdir_attr(self.path)
        except OSError as error:
            translated = self._directory_error(error)
            if translated is None:
                raise
            raise translated from error
        for attr in attrs:
            yield attr.filename, FileStat.from_stat(attr)

    def _entry_stat(self):
        """This entry's stat, or None if even that fails."""
        try:
            return FileStat.from_stat(self._sftpclient.stat(self.path))
        except OSError:
            return None

    def _directory_error(self, error: OSError, *, check_empty=False):
        """pathlib's exception for a failed directory operation (listing,
        `rmdir()`), or None to keep `error`. SFTPv3 has no ENOTDIR or
        ENOTEMPTY status: servers send "no such file" (OpenSSH maps ENOTDIR
        to it) or a bare failure, so the entry itself is consulted -- on this
        failure path only."""
        stat = self._entry_stat()
        if stat is None:
            return None
        if not stat.is_dir():
            return NotADirectoryError(
                _errno.ENOTDIR, _os.strerror(_errno.ENOTDIR), str(self)
            )
        if check_empty and error.errno is None:
            try:
                has_children = bool(self._sftpclient.listdir_attr(self.path))
            except OSError:
                return None
            if has_children:
                return OSError(
                    _errno.ENOTEMPTY, _os.strerror(_errno.ENOTEMPTY), str(self)
                )
        return None

    def _file_error(self, error: OSError):
        """pathlib's exception for a file operation (open, unlink) that
        failed on a directory, or None to keep `error`. SFTPv3 has no EISDIR
        status either: OpenSSH-style servers send a bare failure. A status
        with an errno (permission denied, no such file) is already right."""
        if error.errno is not None:
            return None
        stat = self._entry_stat()
        if stat is None or not stat.is_dir():
            return None
        return IsADirectoryError(_errno.EISDIR, _os.strerror(_errno.EISDIR), str(self))

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
            return self._sftpclient.open(
                self.path, self.backend._wire_open_mode(mode), buffering
            )
        except OSError as error:
            # SFTPv3 has no dedicated "already exists" status code -- an
            # O_EXCL ("x" mode) failure comes back as a generic failure,
            # not the ENOENT-mapped FileNotFoundError already raised
            # correctly for a genuinely missing file/parent. True on both
            # backends against a real-world (v3) server.
            if "x" in mode and self.exists():
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(self)
                ) from error
            translated = self._file_error(error)
            if translated is None:
                raise
            raise translated from error

    def _mkdir(self, mode):
        try:
            return self._sftpclient.mkdir(self.path, mode)
        except OSError as error:
            # Same SFTPv3 status-code gap as _open() above: mkdir on an
            # existing path also comes back as a generic failure.
            if self.exists():
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(self)
                ) from error
            raise

    def chmod(self, mode: int | str, *, follow_symlinks: bool = True):
        mode = _utils.as_mode(mode)
        if follow_symlinks:
            return self._sftpclient.chmod(self.path, mode)
        if not self.backend.supports_lchmod:
            raise NotImplementedError("chmod(follow_symlinks=False)")
        return self._sftpclient.chmod(self.path, mode, follow_symlinks=False)

    def _chown(
        self,
        uid: int | str | None,
        gid: int | str | None,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        # SFTPv3 setstat carries uid/gid as a numeric pair only -- there is
        # no name resolution on the wire, and no way to send just one of
        # them: the protocol's UIDGID flag sets both. So a partial change
        # has to read the current value for the field being left alone,
        # which is why chown() canonicalizes None rather than making every
        # backend invent its own sentinel.
        if isinstance(uid, str) or isinstance(gid, str):
            raise NotImplementedError("sftp chown() requires numeric uid/gid")
        if not follow_symlinks:
            raise NotImplementedError("chown(follow_symlinks=False)")
        if uid is None or gid is None:
            current = self.stat()
            uid = current.st_uid if uid is None else uid
            gid = current.st_gid if gid is None else gid
        return self._sftpclient.chown(self.path, uid, gid)

    def supported_checksums(self) -> "_ty.FrozenSet[str]":
        """`protocols.checksum.NativeChecksum` implementation: delegates to
        `self.backend.supported_checksums(self)`. Empty on the asyncssh
        backend (no client-library support); a real per-connection probe
        on the paramiko backend (see
        `_paramiko.py::SftpBackend.supported_checksums`), so this can be
        empty even on the paramiko backend if the connected server doesn't
        actually implement `check-file-handle` (OpenSSH never does).
        """
        return self.backend.supported_checksums(self)

    def checksum(self, algorithm: str = "md5") -> str:
        """`protocols.checksum.NativeChecksum` implementation: delegates to
        `self.backend.checksum()` (the `check-file-handle` SFTP extension
        on the paramiko backend; unimplemented on asyncssh
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
        # No exists() pre-check: it follows symlinks, so a dangling link
        # read as missing and was never removed (breaking
        # symlink_to(force=True)). pathlib ignores only ENOENT from the call.
        try:
            return self._sftpclient.remove(self.path)
        except FileNotFoundError:
            if not missing_ok:
                raise
        except OSError as error:
            translated = self._file_error(error)
            if translated is None:
                raise
            raise translated from error

    def rmdir(self):
        try:
            return self._sftpclient.rmdir(self.path)
        except OSError as error:
            translated = self._directory_error(error, check_empty=True)
            if translated is None:
                raise
            raise translated from error

    def rename(self, target: "SftpPath | Uri | str"):
        # base Path.rename is the notimplemented stub -- this was never
        # called under its old name `_rename`, so every move() fell back to
        # copy+unlink. `target.path`, not as_posix(): Uri.as_posix() prefixes
        # "host:" for the sftp wire protocol, which only wants the raw path.
        # A plain str target is resolved relative to self's *parent*
        # (sibling rename -- "rename this file to a new name in the same
        # directory"), not to self itself (which would join it as a child)
        # -- and is taken as a literal path rather than re-parsed as a URI,
        # which used to truncate "rn?b.txt" to "rn" on the wire (see
        # `Uri._rename_target`).
        #
        # Replaces an existing target (POSIX rename semantics) through the
        # `posix-rename@openssh.com` extension where the server has it;
        # plain SFTPv3 RENAME refuses to overwrite with a generic failure,
        # which is raised as FileExistsError when the target exists.
        target = self._rename_target(target)
        self._rename_on_wire(target)
        # pathlib returns the new path.
        return self.with_path(target.path)

    def _rename_on_wire(self, target: Uri) -> None:
        client = self._sftpclient
        posix_rename = getattr(client, "posix_rename", None)
        if posix_rename is not None and not _posix_rename_known_unsupported(client):
            try:
                return posix_rename(self.path, target.path)
            except NotImplementedError:
                # asyncssh: the server did not advertise the extension.
                _mark_posix_rename_unsupported(client)
            except OSError as error:
                if error.errno is not None:
                    raise
                # paramiko reports "unsupported" and "failed" alike (a
                # status without errno): a plain rename tells them apart.
                try:
                    result = client.rename(self.path, target.path)
                except OSError as rename_error:
                    self._raise_if_exists(target, rename_error)
                    raise
                _mark_posix_rename_unsupported(client)
                return result
        try:
            return client.rename(self.path, target.path)
        except OSError as error:
            self._raise_if_exists(target, error)
            raise

    def _raise_if_exists(self, target: Uri, error: OSError) -> None:
        if isinstance(error, (FileNotFoundError, PermissionError)):
            return
        try:
            # `target` may be a plain Uri: probe it over this connection.
            probe = self._from_parsed_parts(self.source, target.path, None, None)
            exists = FileStat.from_path(probe, follow_symlink=False) is not None
        except Exception:
            return
        if exists:
            raise FileExistsError(
                _errno.EEXIST, "File exists", str(target.path)
            ) from error

    def _symlink_to(
        self, target: "SftpPath | Uri", target_is_directory: bool = False
    ) -> None:
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
        #
        # Verbatim either way (no dot-segment folding). A relative target
        # carries no host: with this path's authority it had no printable
        # URI at all, so str()/repr()/== raised ValueError.
        target = self._sftpclient.readlink(self.path)
        source = self.source if target.startswith("/") else _NOSOURCE
        return self._from_parsed_parts(source, target, None, None)

    def hardlink_to(self, target: "SftpPath | Uri | str"):
        if not self.backend.supports_hardlink:
            raise NotImplementedError("hardlink_to() requires the asyncssh backend")
        if isinstance(target, Uri) and not self._same_location(target):
            raise NotImplementedError(
                f"hardlink_to() cannot link across hosts: {self} -> {target}"
            )
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

        # Connect on THIS thread: the coroutine runs on the bridge loop,
        # where opening the connection would block the loop it needs.
        aclient = self._sftpclient._aclient
        # A whole-tree operation: no wall-clock bound (single requests are).
        return _run(
            _concurrent_rm(
                self,
                max_concurrency=self.backend.max_concurrency,
                missing_ok=missing_ok,
                on_error=on_error,
                aclient=aclient,
            ),
            None,
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
        directory, child copies are fanned out as concurrent native requests,
        bounded by `backend.max_concurrency` (requests in flight, and files
        open at once); the whole copy has no wall-clock timeout. `progress`
        is honored on the
        generic single-file fallback path below, but **not** called during
        the concurrent native fan-out itself -- see `docs/divergences.md`'s
        "Deliberate extensions" section for the documented limitation.
        """
        try:
            from ._asyncssh import AsyncsshSftpBackend, _concurrent_copy, _run
        except ImportError:
            # paramiko-only install ('sftp' extra): generic copy.
            AsyncsshSftpBackend = None

        if (
            AsyncsshSftpBackend is None
            or not isinstance(self.backend, AsyncsshSftpBackend)
            or not recursive
            # The fan-out writes every destination file over THIS path's
            # connection: only a target on the same host may use it.
            or not isinstance(target, SftpPath)
            or not self._same_location(target)
            # A link copied as a link is the generic copy's job.
            or (not follow_symlinks and self.is_symlink())
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

        if _contains(self, target):
            # As the generic copy(): checked before anything is created, or
            # the new directory is listed and copied into itself.
            raise OSError(
                _errno.EINVAL, "Cannot copy a directory into itself", str(target)
            )

        if target.exists():
            if not target.is_dir():
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(target)
                )
            if not overwrite:
                raise FileExistsError(
                    _errno.EEXIST, _os.strerror(_errno.EEXIST), str(target)
                )
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
            # Resolved on this thread, never on the bridge loop (see rm()).
            aclient=self._sftpclient._aclient,
        )
        return _run(coro, None)
