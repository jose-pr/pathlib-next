from __future__ import annotations

import calendar as _calendar
import contextlib as _contextlib
import errno as _errno
import ftplib as _ftplib
import io as _io
import ssl as _ssl
import stat as _stat
import threading as _thread
import time as _time
import typing as _ty

import netimps as _netimps

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import Source, Uri, UriPath


class BaseFtpBackend(object):
    """Protocol for obtaining a connected+logged-in `ftplib.FTP` (or
    `FTP_TLS`) for a `Source`. Subclass this to plug in custom connection
    handling (e.g. tests mock it directly, no real server);
    `FtpBackend` is the real implementation."""

    __slots__ = ()

    @_utils.notimplemented
    def client(self, source: Source, tls: bool) -> "_ftplib.FTP": ...


DEFAULT_TIMEOUT = 30.0
"""Socket timeout, in seconds, `FtpBackend` applies to connect, replies and
transfers when the caller does not pass one."""


class _SessionReuseFTP_TLS(_ftplib.FTP_TLS):
    """`FTP_TLS` whose data connections resume the control connection's TLS
    session. Stdlib `ntransfercmd` wraps the data socket without
    `session=`, and servers such as vsftpd (`require_ssl_reuse=YES`, the
    default) and FileZilla Server reject such a data connection with
    `522`. With TLS 1.3 the session ticket arrives after the handshake; by
    the first transfer the login replies have been read, so it is there."""

    def ntransfercmd(self, cmd, rest=None):
        # Skip FTP_TLS.ntransfercmd (it wraps without `session=`).
        conn, size = super(_ftplib.FTP_TLS, self).ntransfercmd(cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=getattr(self.sock, "session", None),
            )
        return conn, size


class FtpBackend(BaseFtpBackend):
    """Connects via stdlib `ftplib.FTP` (`ftp:`) or `ftplib.FTP_TLS`
    (`ftps:`, with `PROT P` for an encrypted data channel too).

    `timeout` (seconds, default `DEFAULT_TIMEOUT` = 30) bounds connect,
    every reply and every transfer read; `None` blocks forever.

    `ftps:` verifies the server certificate and host name by default
    (`ssl.create_default_context()`), before `USER`/`PASS` are sent. To
    trust a private CA or a self-signed certificate, pass your own
    `ssl_context` (e.g. `ssl.create_default_context(cafile=...)`). To turn
    verification off entirely -- accepting any certificate, so anyone on
    the network path can read the password -- pass `verify=False`.
    `ssl_context` wins over `verify` when both are given. Data connections
    reuse the control connection's TLS session."""

    __slots__ = ("timeout", "ssl_context", "verify")

    def __init__(
        self,
        timeout: "float | None" = DEFAULT_TIMEOUT,
        ssl_context: "_ssl.SSLContext | None" = None,
        verify: bool = True,
    ) -> None:
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.verify = verify

    def _tls_context(self) -> "_ssl.SSLContext":
        if self.ssl_context is not None:
            return self.ssl_context
        context = _ssl.create_default_context()
        if not self.verify:
            context.check_hostname = False
            context.verify_mode = _ssl.CERT_NONE
        return context

    def client(self, source: Source, tls: bool):
        if tls:
            client = _SessionReuseFTP_TLS(
                context=self._tls_context(), timeout=self.timeout
            )
        else:
            client = _ftplib.FTP(timeout=self.timeout)
        try:
            client.connect(
                str(source.host), source.port or _netimps.get_default_port("ftp")
            )
            user, password = source.parsed_userinfo()
            client.login(user or "anonymous", password or "")
            if tls:
                client.prot_p()
            client.set_pasv(True)
        except BaseException:
            # A rejected certificate or a timed-out greeting must not leave
            # the half-open control socket behind.
            close = getattr(client, "close", None)
            if close is not None:
                close()
            raise
        return client


def _create_ftpclient(
    backend: BaseFtpBackend, source: Source, tls: bool, thread_id: int
):
    return backend.client(source, tls)


def _close_ftpclient(key: tuple, client: "_ftplib.FTP") -> None:
    # Only the calling thread's own connections are closed here: an LRU
    # overflow can evict another thread's entry while that thread is in the
    # middle of a transfer on it. Those are dropped from the cache and close
    # when their last user lets go of them.
    if key[3] == _thread.get_ident():
        client.close()


# Keyed by (backend, source, tls, thread): ftplib clients are not
# thread-safe. Evicted and discarded clients of the calling thread are
# closed (`_close_ftpclient`), not left logged in.
_CACHED_CLIENTS = _utils.LRU(_create_ftpclient, maxsize=128, on_evict=_close_ftpclient)

_DEFAULT_BACKEND = FtpBackend()
"""The backend every `FtpPath` built without `backend=` shares, so separately
constructed paths to one server reuse one connection per thread instead of
opening (and keeping) one each."""

# Reply codes meaning "command not implemented" (RFC 959): the server lacks
# the command, as opposed to refusing it for this path.
_UNSUPPORTED_REPLIES = ("500", "502", "504")

_PERMISSION_WORDS = ("permission", "privilege", "denied", "not allowed", "access")


def _reply_code(error: BaseException) -> str:
    return str(error)[:3]


def _parse_mlsd_time(value: str) -> int:
    # MLSD "modify" fact: YYYYMMDDHHMMSS[.sss], always UTC (RFC 3659) --
    # timegm, not datetime.timestamp(), which reads a naive time as local.
    try:
        return _calendar.timegm(_time.strptime(value[:14], "%Y%m%d%H%M%S"))
    except ValueError:
        return 0


def _facts_mode(facts: dict, is_dir: bool) -> "int | None":
    """`st_mode` from MLSD facts: `unix.mode` verbatim when the server sends
    it, else the RFC 3659 `perm` fact (the login user's rights) as
    read/write bits. None when neither fact is present."""
    kind = _stat.S_IFDIR if is_dir else _stat.S_IFREG
    unix_mode = facts.get("unix.mode")
    if unix_mode:
        try:
            return kind | (int(unix_mode, 8) & 0o7777)
        except ValueError:
            pass
    perm = facts.get("perm")
    if perm is None:
        return None
    perm = set(perm.lower())
    if is_dir:
        bits = (0o555 if perm & set("el") else 0) | (0o200 if perm & set("cmp") else 0)
    else:
        bits = (0o444 if "r" in perm else 0) | (0o200 if perm & set("aw") else 0)
    return kind | bits


class _FtpWriteStream(_io.BytesIO):
    """Buffers the whole write in memory, uploads on close() via
    STOR/APPE. Simple and works with any ftplib client, at the cost of
    holding the full file content in memory for the duration of the write.

    The connection is looked up at close(), not captured at open(): a write
    that outlasts the server's idle timeout still lands, on a reconnected
    session. With `initial` (`open("r+")`) the buffer starts with the
    file's content at position 0 and is uploaded only if it was modified."""

    def __init__(
        self, path: "FtpPath", append: bool = False, initial: "bytes | None" = None
    ):
        super().__init__(b"" if initial is None else initial)
        self._path = path
        self._cmd = "APPE" if append else "STOR"
        self._dirty = initial is None

    def write(self, data):
        self._dirty = True
        return super().write(data)

    def writelines(self, lines):
        self._dirty = True
        return super().writelines(lines)

    def truncate(self, size=None):
        self._dirty = True
        return super().truncate(size)

    def close(self):
        if self.closed:
            return
        try:
            if self._dirty:
                self.seek(0)
                self._path._store(self._cmd, self)
        finally:
            # Closed even when the upload fails, so `IOBase.__del__` does not
            # retry it (over newer content) at garbage collection.
            super().close()


class FtpPath(UriPath):
    """`ftp:`/`ftps:` scheme: full read/write access via stdlib `ftplib`,
    with a thread-keyed LRU connection cache (`_CACHED_CLIENTS`, mirroring
    `sftp.py`). Directory listing and stat prefer MLSD (RFC 3659 -- gives
    type/size/modify facts in one round trip); servers that don't support it
    fall back to NLST for listing (names only) and SIZE for file stat
    (no portable "not found vs. is a directory" distinction in that path)."""

    __SCHEMES = ("ftp", "ftps")
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseFtpBackend

    def _initbackend(self):
        return _DEFAULT_BACKEND

    @property
    def _tls(self):
        return self.source.scheme == "ftps"

    @property
    def _ftpclient(self) -> "_ftplib.FTP":
        thread_id = _thread.get_ident()
        client = _CACHED_CLIENTS(self.backend, self.source, self._tls, thread_id)
        try:
            client.voidcmd("NOOP")
        except (
            OSError,
            EOFError,
            _ftplib.error_temp,
            _ftplib.error_proto,
            _ftplib.error_reply,
        ):
            # Replaced and closed (`_close_ftpclient`), not abandoned.
            client = _CACHED_CLIENTS.invalidate(
                self.backend, self.source, self._tls, thread_id
            )
        return client

    @_contextlib.contextmanager
    def _wire(self):
        """The calling thread's live client, for one command or transfer.

        A complete error reply (`error_perm`/`error_temp`) leaves the control
        channel in step and passes through for the caller to translate.
        Anything else raised while the client is in use -- a dropped
        connection, an unexpected reply, a KeyboardInterrupt or decode error
        in the middle of a transfer whose final reply is still queued --
        leaves it out of step, so the client is dropped from the cache and
        closed; the next operation reconnects. Protocol errors and EOF are
        re-raised as ConnectionError."""
        key = (self.backend, self.source, self._tls, _thread.get_ident())
        try:
            client = self._ftpclient
        except _ftplib.error_perm as error:
            # A refused login is not a refusal for this path: it must not
            # reach `_translate()`'s probes and read as "no such file".
            raise PermissionError(
                _errno.EACCES, f"FTP login refused: {error}", str(self)
            ) from error
        except (_ftplib.Error, EOFError) as error:
            raise ConnectionError(
                _errno.ECONNREFUSED, f"FTP connection failed: {error!r}", str(self)
            ) from error
        try:
            yield client
        except (_ftplib.error_perm, _ftplib.error_temp):
            raise
        except BaseException as error:
            with _CACHED_CLIENTS.lock:
                cached = _CACHED_CLIENTS.cache.get(key) is client
                if cached:
                    _CACHED_CLIENTS.discard(*key)
            if not cached:
                client.close()
            if isinstance(error, (_ftplib.Error, EOFError)):
                raise ConnectionError(
                    _errno.ECONNABORTED, f"FTP session failed: {error!r}", str(self)
                ) from error
            raise

    def _call(self, method: str, *args):
        """`client.<method>(*args)` through `_wire()`. Only `error_perm` is
        left for the caller; a transient `error_temp` becomes OSError."""
        try:
            with self._wire() as client:
                return getattr(client, method)(*args)
        except _ftplib.error_temp as error:
            raise OSError(_errno.EAGAIN, str(error), str(self)) from error

    def _translate(self, error: "_ftplib.error_perm", wrong_type=None) -> OSError:
        """OSError for a refused command on this path. A reply alone cannot
        say why: a read-only login gets "550 Not enough privileges" for a
        missing file too. So the path is stat'ed: missing is
        FileNotFoundError; `wrong_type(stat)` may name a type mismatch
        (IsADirectoryError, ...); anything else is PermissionError."""
        try:
            st = self._fresh_stat()
        except FileNotFoundError:
            return FileNotFoundError(
                _errno.ENOENT, f"No such file or directory ({error})", str(self)
            )
        except OSError:
            st = None
        if st is not None and wrong_type is not None:
            mismatch = wrong_type(st)
            if mismatch is not None:
                return mismatch
        return PermissionError(_errno.EACCES, str(error), str(self))

    def _mlsd_entry(self):
        """This entry's MLSD facts from its parent's listing, or None if
        not found, the parent can't be listed, or the server doesn't
        support MLSD."""
        parent = self.path.rsplit("/", 1)[0] or "/"
        try:
            for name, facts in self._call("mlsd", parent):
                if name == self.name:
                    return facts
        except _ftplib.error_perm:
            return None
        return None

    def _facts_to_filestat(self, facts: dict) -> FileStat:
        kind = facts.get("type", "file")
        size = int(facts.get("size", 0) or 0)
        modify = facts.get("modify")
        mtime = _parse_mlsd_time(modify) if modify else 0
        is_dir = kind in ("dir", "cdir", "pdir")
        return FileStat(
            st_mode=_facts_mode(facts, is_dir),
            st_size=size,
            st_mtime=mtime,
            is_dir=is_dir,
        )

    def _not_a_directory(self, st) -> "OSError | None":
        if st.is_dir():
            return None
        return NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))

    def _is_a_directory(self, st) -> "OSError | None":
        if not st.is_dir():
            return None
        return IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))

    def _scandir(self):
        # MLSD's facts already carry type/size/modify for every child in
        # one round trip -- reuse them instead of `iterdir()` + a separate
        # stat per child. Falls back to NLST (names only, no metadata) on
        # servers that don't support MLSD. The listing is read whole before
        # anything is yielded, so no transfer is left half-read.
        try:
            listing = [
                (name, self._facts_to_filestat(facts))
                for name, facts in self._call("mlsd", self.path)
                if name not in (".", "..")
            ]
        except _ftplib.error_perm as error:
            if _reply_code(error) not in _UNSUPPORTED_REPLIES:
                # A missing path, a file, or a refused listing.
                raise self._translate(error, self._not_a_directory) from error
            try:
                names = self._call("nlst", self.path)
            except _ftplib.error_perm as error:
                raise self._translate(error, self._not_a_directory) from error
            listing = [
                (base, None)
                for base in (name.rsplit("/", 1)[-1] for name in names)
                if base not in (".", "..")
            ]
            if len(listing) <= 1:
                # Servers answer NLST of a missing path with an empty list,
                # and of a file with the file's own name.
                mismatch = self._not_a_directory(self._fresh_stat())
                if mismatch is not None:
                    raise mismatch
        yield from listing

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        return self._fresh_stat()

    def _fresh_stat(self) -> FileStat:
        # Special case: the FTP root '/' has no parent to MLSD and SIZE won't
        # work on a directory.  Confirm it exists via CWD.
        if self.path in ("/", ""):
            try:
                self._call("cwd", "/")
                return FileStat(is_dir=True)
            except _ftplib.error_perm as error:
                raise FileNotFoundError(self) from error
        facts = self._mlsd_entry()
        if facts is not None:
            return self._facts_to_filestat(facts)
        # MLSD unsupported, the parent unlistable, or the entry not in it.
        # SIZE answers for a file -- in binary mode: servers refuse it in
        # the ASCII mode a fresh or listing session is in. CWD answers for
        # a directory (every path here is absolute, so the changed working
        # directory affects nothing).
        try:
            self._call("voidcmd", "TYPE I")
            size = self._call("size", self.path)
        except _ftplib.error_perm:
            size = None
        if size is not None:
            return FileStat(st_size=size, is_dir=False)
        try:
            self._call("cwd", self.path)
        except _ftplib.error_perm as error:
            raise FileNotFoundError(self) from error
        return FileStat(is_dir=True)

    def _open(self, mode="r", buffering=-1):
        if mode in ("r", "r+"):
            buf = _io.BytesIO()
            try:
                self._call("retrbinary", f"RETR {self.path}", buf.write)
            except _ftplib.error_perm as error:
                raise self._translate(error, self._is_a_directory) from error
            if mode == "r+":
                # Read-modify-write: what is written is uploaded on close.
                return _FtpWriteStream(self, initial=buf.getvalue())
            buf.seek(0)
            # Read-only, like a local file opened "rb": a write raises
            # instead of landing in a buffer nobody uploads.
            return _io.BufferedReader(buf)
        if mode not in ("w", "x", "a"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _FtpWriteStream(self, append=(mode == "a"))

    def _store(self, cmd: str, fileobj) -> None:
        """Upload `fileobj` with STOR/APPE on a live connection."""
        try:
            self._call("storbinary", f"{cmd} {self.path}", fileobj)
        except _ftplib.error_perm as error:
            raise self._create_error(error) from error

    def _create_error(self, error: "_ftplib.error_perm") -> OSError:
        """OSError for a refused STOR/MKD of this path: an existing
        directory, a missing or non-directory parent, or a refusal."""
        try:
            if self._fresh_stat().is_dir():
                return IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        except FileNotFoundError:
            pass
        except OSError:
            return PermissionError(_errno.EACCES, str(error), str(self))
        parent = self.parent
        if parent != self:
            try:
                parent_stat = parent._fresh_stat()
            except FileNotFoundError:
                return FileNotFoundError(
                    _errno.ENOENT, f"No such file or directory ({error})", str(self)
                )
            except OSError:
                parent_stat = None
            if parent_stat is not None and not parent_stat.is_dir():
                return NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
        return PermissionError(_errno.EACCES, str(error), str(self))

    def _mkdir(self, mode):
        try:
            self._call("mkd", self.path)
        except _ftplib.error_perm as error:
            if self.exists():
                raise FileExistsError(
                    _errno.EEXIST, "File exists", str(self)
                ) from error
            raise self._create_error(error) from error

    def unlink(self, missing_ok=False):
        try:
            self._call("delete", self.path)
        except _ftplib.error_perm as error:
            translated = self._translate(error, self._is_a_directory)
            if missing_ok and isinstance(translated, FileNotFoundError):
                return
            raise translated from error

    def rmdir(self):
        try:
            self._call("rmd", self.path)
        except _ftplib.error_perm as error:
            raise self._translate(error, self._rmdir_mismatch(error)) from error

    def _rmdir_mismatch(self, error: "_ftplib.error_perm"):
        def mismatch(st) -> "OSError | None":
            if not st.is_dir():
                return NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
            if any(word in str(error).lower() for word in _PERMISSION_WORDS):
                return None
            try:
                empty = next(iter(self._scandir()), None) is None
            except OSError:
                return None
            if not empty:
                return OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
            return None

        return mismatch

    def rename(self, target: "FtpPath | Uri | str"):
        # A plain str target is a sibling rename (relative to self's
        # parent), matching sftp.py's rename() semantics.
        target = self._rename_target(target)
        try:
            self._call("rename", self.path, target.path)
        except _ftplib.error_perm as error:
            raise self._translate(error) from error

    def chmod(self, mode: int | str, *, follow_symlinks: bool = True):
        # SITE CHMOD is a non-standard FTP extension; pyftpdlib and many real
        # servers do not support it.  A server rejection of an existing path
        # becomes NotImplementedError so that path.py's copy() silently skips
        # it; a missing path is FileNotFoundError.
        if not follow_symlinks:
            raise NotImplementedError("chmod(follow_symlinks=False)")
        mode = _utils.as_mode(mode)
        try:
            self._call("voidcmd", f"SITE CHMOD {mode:o} {self.path}")
        except _ftplib.error_perm as error:
            if _reply_code(error) not in _UNSUPPORTED_REPLIES:
                translated = self._translate(error)
                if isinstance(translated, FileNotFoundError):
                    raise translated from error
            raise NotImplementedError("SITE CHMOD not supported by this server")
