from __future__ import annotations

import calendar as _calendar
import contextlib as _contextlib
import errno as _errno
import ftplib as _ftplib
import io as _io
import itertools as _itertools
import ssl as _ssl
import stat as _stat
import threading as _thread
import time as _time
import types as _types
import typing as _ty
import weakref as _weakref

import netimps as _netimps

from ... import utils as _utils
from ...utils._commit import _CommitOnClose
from ...utils.stat import FileStat
from .. import Source, Uri, UriPath


class BaseFtpBackend(object):
    """Protocol for obtaining a connected+logged-in `ftplib.FTP` (or
    `FTP_TLS`) for a `Source`. Subclass this to plug in custom connection
    handling (e.g. tests mock it directly, no real server);
    `FtpBackend` is the real implementation."""

    # Weakly referenceable so `UriPath` can record which backends a path
    # derived for itself (see `UriPath._supplied_backend()`).
    __slots__ = ("__weakref__",)

    @_utils.notimplemented
    def client(self, source: Source, tls: bool) -> "_ftplib.FTP":
        """The connected, logged-in client for `source`; `tls` asks for `ftps:`."""


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


IDLE_PROBE_SECONDS = 1.0
"""A cached control connection that carried a command within this many seconds
is used as it is. One that has been idle longer, or has not carried a command
yet, is probed with `NOOP` first and replaced if the server dropped it.
Servers close idle sessions after tens of seconds at the least, so a run of
operations pays no probe and a pause long enough to lose the session pays one."""

_now = _time.monotonic


class _Session:
    """What is known of one cached connection: when it last answered, whether
    the server offers `MLST`, and whether a command is running on it. A
    connection dropped from the cache while a command runs on it is closed
    when that command ends."""

    __slots__ = ("last_used", "mlst", "busy", "retired")

    def __init__(self):
        self.last_used: "float | None" = None
        self.mlst: "bool | None" = None
        self.busy = 0
        self.retired = False


_SESSION_ATTR = "_pathlib_next_session"
_SESSION_LOCK = _thread.Lock()


def _session_of(client) -> "_Session | None":
    session = getattr(client, _SESSION_ATTR, None)
    if session is None:
        session = _Session()
        try:
            setattr(client, _SESSION_ATTR, session)
        except AttributeError:
            return None  # a client that takes no attributes is always probed
    return session


def _create_ftpclient(backend: BaseFtpBackend, source: Source, tls: bool, scope: int):
    return backend.client(source, tls)


def _close_ftpclient(key: tuple, client: "_ftplib.FTP") -> None:
    session = getattr(client, _SESSION_ATTR, None)
    if session is not None:
        with _SESSION_LOCK:
            if session.busy:
                # Dropped from the cache while a command is running on it
                # (another thread's LRU overflow): its user closes it.
                session.retired = True
                return
    client.close()


_THREAD_SCOPE = _thread.local()
_SCOPE_IDS = _itertools.count(1)


def _drop_scope(scope: int) -> None:
    with _CACHED_CLIENTS.lock:
        keys = [key for key in _CACHED_CLIENTS.cache if key[3] == scope]
    for key in keys:
        _CACHED_CLIENTS.discard(*key)


class _ThreadScope:
    """Held in the thread's own storage, so it is released when the thread
    ends and takes the thread's cached connections with it."""

    def __init__(self):
        self.id = next(_SCOPE_IDS)
        _weakref.finalize(self, _drop_scope, self.id).atexit = False


def _scope_id() -> int:
    """The calling thread's key in `_CACHED_CLIENTS`. Not the thread
    identifier, which the operating system reuses for a later thread."""
    scope = getattr(_THREAD_SCOPE, "scope", None)
    if scope is None:
        scope = _THREAD_SCOPE.scope = _ThreadScope()
    return scope.id


# Keyed by (backend, source, tls, thread scope): ftplib clients are not
# thread-safe. Evicted and discarded clients are closed (`_close_ftpclient`),
# not left logged in, and a thread's clients are dropped when it ends.
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


def _is_child_entry(name: str, facts: dict) -> bool:
    """Whether an MLSD entry names a child of the listed directory. The
    `cdir`/`pdir` entries describe the directory itself and its parent, and
    may carry any name -- RFC 3659's own example lists `tmp` and `/tmp` --
    so they are skipped by type, not only as `.`/`..`; a name that is not one
    path component (`utils.is_safe_child_name()`) is not a child name either."""
    kind = str(facts.get("type", "")).lower()
    return kind not in ("cdir", "pdir") and _utils.is_safe_child_name(name)


def _parse_mlsd_time(value: str) -> int:
    # MLSD "modify" fact: YYYYMMDDHHMMSS[.sss], always UTC (RFC 3659) --
    # timegm, not datetime.timestamp(), which reads a naive time as local.
    try:
        return _calendar.timegm(_time.strptime(value[:14], "%Y%m%d%H%M%S"))
    except ValueError:
        return 0


def _facts_size(value) -> int:
    """`st_size` from an MLSD `size` fact. A value that is not a plain
    non-negative decimal number, or too large for any file, is unknown (0), as
    a missing fact is."""
    text = str(value or "")
    if not (text.isascii() and text.isdigit()):
        return 0
    size = int(text)
    return size if size < 1 << 63 else 0


def _parse_fact_line(line: str) -> "tuple[str, dict]":
    """`(name, facts)` of one RFC 3659 `facts SP name` line; fact names are
    lower-cased, as `ftplib.FTP.mlsd()` does."""
    found, _, name = line.rstrip("\r\n").partition(" ")
    facts = {}
    for fact in found[:-1].split(";") if found else ():
        key, _, value = fact.partition("=")
        facts[key.lower()] = value
    return name, facts


def _lists_mlst(reply: str) -> bool:
    """Whether a `FEAT` reply has an `MLST` feature line (RFC 2389)."""
    for line in reply.splitlines():
        words = line.split(None, 1)
        if line[:1] == " " and words and words[0].upper() == "MLST":
            return True
    return False


def _mlst_facts(reply: str) -> "dict | None":
    """The facts of the entry in an `MLST` reply (RFC 3659 7.2: `250-` line,
    one entry line starting with a space, `250` line), or None when it holds
    no entry that says what it is."""
    for line in reply.splitlines()[1:]:
        if line[:1] == " ":
            _name, facts = _parse_fact_line(line[1:])
            return facts if "type" in facts else None
    return None


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


class FtpPath(UriPath):
    """`ftp:`/`ftps:` scheme: full read/write access via stdlib `ftplib`,
    with a thread-keyed LRU connection cache (`_CACHED_CLIENTS`, mirroring
    `sftp.py`; a thread's connections close when it ends). A listing prefers
    MLSD (RFC 3659 -- type/size/modify facts in one round trip) and a stat
    MLST of the entry itself where FEAT lists it, else its parent's MLSD;
    servers that don't support them fall back to NLST for listing (names
    only) and SIZE/CWD for stat (no portable "not found vs. is a directory"
    distinction in that path)."""

    __SCHEMES = ("ftp", "ftps")
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseFtpBackend

    def _initbackend(self):
        return _DEFAULT_BACKEND

    @property
    def _tls(self):
        return self.source.scheme == "ftps"

    def _cache_key(self) -> tuple:
        return (self.backend, self.source, self._tls, _scope_id())

    @property
    def _ftpclient(self) -> "_ftplib.FTP":
        key = self._cache_key()
        client = _CACHED_CLIENTS(*key)
        session = _session_of(client)
        if (
            session is not None
            and session.last_used is not None
            and _now() - session.last_used <= IDLE_PROBE_SECONDS
        ):
            return client
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
            client = _CACHED_CLIENTS.invalidate(*key)
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
        key = self._cache_key()
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
        session = _session_of(client)
        if session is not None:
            with _SESSION_LOCK:
                session.busy += 1
        try:
            yield client
            if session is not None:
                session.last_used = _now()
        except (_ftplib.error_perm, _ftplib.error_temp):
            # A complete reply: the connection is in step.
            if session is not None:
                session.last_used = _now()
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
            if isinstance(error, UnicodeDecodeError):
                raise OSError(
                    _errno.EILSEQ,
                    "FTP listing is not valid "
                    f"{getattr(client, 'encoding', 'utf-8')}: {error}",
                    str(self),
                ) from error
            raise
        finally:
            if session is not None:
                with _SESSION_LOCK:
                    session.busy -= 1
                    closing = session.retired and not session.busy
                if closing:
                    client.close()

    def _call(self, method: str, *args):
        """`client.<method>(*args)` through `_wire()`. Only `error_perm` is
        left for the caller; a transient `error_temp` becomes OSError.

        `FTP.mlsd()` is a generator that runs its transfer while it is
        iterated, so a listing is read whole here, inside the guard: a
        failure half way through it evicts the client like any other."""
        try:
            with self._wire() as client:
                result = getattr(client, method)(*args)
                if isinstance(result, _types.GeneratorType):
                    result = list(result)
                return result
        except _ftplib.error_temp as error:
            raise OSError(_errno.EAGAIN, str(error), str(self)) from error

    @property
    def _wirepath(self) -> str:
        """This path as a command argument. An empty path (`ftp://host`) is
        the root, as `ftp://host/` is, and an empty argument would name the
        session's working directory instead."""
        return self.path or "/"

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
        not found, the parent can't be listed (a name in it the client cannot
        decode included), or the server doesn't support MLSD."""
        parent = self.path.rsplit("/", 1)[0] or "/"
        try:
            for name, facts in self._call("mlsd", parent):
                if name == self.name and _is_child_entry(name, facts):
                    return facts
        except _ftplib.error_perm:
            return None
        except OSError as error:
            if error.errno == _errno.EILSEQ:
                return None
            raise
        return None

    def _offers_mlst(self) -> bool:
        """Whether the server of this path's connection lists `MLST` in its
        `FEAT` reply, asked once per connection."""
        try:
            with self._wire() as client:
                session = _session_of(client)
                if session is not None and session.mlst is not None:
                    return session.mlst
                send = getattr(client, "sendcmd", None)
                offered = False
                if send is not None:
                    try:
                        offered = _lists_mlst(send("FEAT"))
                    except _ftplib.error_perm:
                        pass  # 500/502: the server has no FEAT
                if session is not None:
                    session.mlst = offered
                return offered
        except _ftplib.error_temp:
            return False  # not remembered: the next stat asks again

    def _stat_entry(self):
        """This entry's facts, asked of the server in the fewest replies it
        allows: `MLST` of the entry itself where `FEAT` offers it (RFC 3659
        7.2), else its parent's `MLSD` listing. None when the server has no
        such entry, or will not say."""
        if self._offers_mlst():
            try:
                reply = self._control(f"MLST {self._wirepath}")
            except _ftplib.error_perm:
                return None
            facts = None if reply is None else _mlst_facts(reply)
            if facts is not None:
                return facts
            # An MLST that answers without an entry is no use on this
            # connection: use the listing, and do not ask again.
            session = self._cached_session()
            if session is not None:
                session.mlst = False
        return self._mlsd_entry()

    def _cached_session(self) -> "_Session | None":
        client = _CACHED_CLIENTS.cache.get(self._cache_key())
        return None if client is None else _session_of(client)

    def _control(self, command: str) -> "str | None":
        """The reply to a raw `command` on the control connection, or None for
        a client that cannot send one. `error_perm` is left for the caller; a
        transient `error_temp` becomes OSError."""
        try:
            with self._wire() as client:
                send = getattr(client, "sendcmd", None)
                return None if send is None else send(command)
        except _ftplib.error_temp as error:
            raise OSError(_errno.EAGAIN, str(error), str(self)) from error

    def _facts_to_filestat(self, facts: dict) -> FileStat:
        # Fact values are case-insensitive (RFC 3659 7.5.1).
        kind = str(facts.get("type", "file")).lower()
        size = _facts_size(facts.get("size"))
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
                for name, facts in self._call("mlsd", self._wirepath)
                if _is_child_entry(name, facts)
            ]
            if (
                len(listing) == 1
                and listing[0][0] == self.name
                and not listing[0][1].is_dir()
            ):
                # RFC 3659 has MLSD of a file refused (501), but a server
                # that lists the file itself instead looks the same as a
                # directory holding one same-named file: only a stat tells.
                mismatch = self._not_a_directory(self._fresh_stat())
                if mismatch is not None:
                    raise mismatch
        except _ftplib.error_perm as error:
            if _reply_code(error) not in _UNSUPPORTED_REPLIES:
                # A missing path, a file, or a refused listing.
                raise self._translate(error, self._not_a_directory) from error
            try:
                names = self._call("nlst", self._wirepath)
            except _ftplib.error_perm as error:
                raise self._translate(error, self._not_a_directory) from error
            listing = [
                (base, None)
                for base in (name.rsplit("/", 1)[-1] for name in names)
                if _utils.is_safe_child_name(base)
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
        facts = self._stat_entry()
        if facts is not None:
            return self._facts_to_filestat(facts)
        # MLST/MLSD unsupported, the parent unlistable, or the entry not there.
        # SIZE answers for a file -- in binary mode: servers refuse it in
        # the ASCII mode a fresh or listing session is in. CWD answers for
        # a directory (every path here is absolute, so the changed working
        # directory affects nothing).
        try:
            self._call("voidcmd", "TYPE I")
            size = self._call("size", self._wirepath)
        except _ftplib.error_perm:
            size = None
        if size is not None:
            return FileStat(st_size=size, is_dir=False)
        try:
            self._call("cwd", self._wirepath)
        except _ftplib.error_perm as error:
            raise FileNotFoundError(self) from error
        return FileStat(is_dir=True)

    def _open(self, mode="r", buffering=-1):
        if mode in ("r", "r+"):
            buf = _io.BytesIO()
            try:
                self._call("retrbinary", f"RETR {self._wirepath}", buf.write)
            except _ftplib.error_perm as error:
                raise self._translate(error, self._is_a_directory) from error
            if mode == "r+":
                # Read-modify-write: what is written is uploaded on close.
                return self._upload_on_close("STOR", initial=buf.getvalue())
            buf.seek(0)
            # Read-only, like a local file opened "rb": a write raises
            # instead of landing in a buffer nobody uploads.
            return _io.BufferedReader(buf)
        if mode not in ("w", "x", "a"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return self._upload_on_close("APPE" if mode == "a" else "STOR")

    def _upload_on_close(self, cmd: str, initial: "bytes | None" = None):
        """A write buffer that uploads with `cmd` (STOR/APPE) when it is
        closed. The connection is looked up then, not when the file is opened:
        a write that outlasts the server's idle timeout still lands, on a
        reconnected session."""

        def upload(buffer):
            buffer.seek(0)
            self._store(cmd, buffer)

        return _CommitOnClose(upload, initial)

    def _store(self, cmd: str, fileobj) -> None:
        """Upload `fileobj` with STOR/APPE on a live connection."""
        try:
            self._call("storbinary", f"{cmd} {self._wirepath}", fileobj)
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
            self._call("mkd", self._wirepath)
        except _ftplib.error_perm as error:
            if self.exists():
                raise FileExistsError(
                    _errno.EEXIST, "File exists", str(self)
                ) from error
            raise self._create_error(error) from error

    def unlink(self, missing_ok=False):
        try:
            self._call("delete", self._wirepath)
        except _ftplib.error_perm as error:
            translated = self._translate(error, self._is_a_directory)
            if missing_ok and isinstance(translated, FileNotFoundError):
                return
            raise translated from error

    def rmdir(self):
        try:
            self._call("rmd", self._wirepath)
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
        destination = self.with_path(target.path)
        try:
            self._call("rename", self._wirepath, target.path or "/")
        except _ftplib.error_perm as error:
            raise self._rename_error(error, destination) from error
        # pathlib returns the new path.
        return destination

    def _rename_error(
        self, error: "_ftplib.error_perm", destination: "FtpPath"
    ) -> OSError:
        """OSError for a refused rename. Whether a server replaces an existing
        target is its own business (POSIX ones do, Windows ones answer `550
        File exists`), so a refusal that does not read as a permission one,
        with the source there and the target too, is `FileExistsError`."""
        refused = self._translate(error)
        if isinstance(refused, FileNotFoundError) or any(
            word in str(error).lower() for word in _PERMISSION_WORDS
        ):
            return refused
        try:
            destination._fresh_stat()
        except OSError:
            return refused
        return FileExistsError(
            _errno.EEXIST, f"File exists ({error})", str(destination)
        )

    def chmod(self, mode: int | str, *, follow_symlinks: bool = True):
        # SITE CHMOD is a non-standard FTP extension; pyftpdlib and many real
        # servers do not support it.  A server rejection of an existing path
        # becomes NotImplementedError so that path.py's copy() silently skips
        # it; a missing path is FileNotFoundError.
        if not follow_symlinks:
            raise NotImplementedError("chmod(follow_symlinks=False)")
        mode = _utils.as_mode(mode)
        try:
            self._call("voidcmd", f"SITE CHMOD {mode:o} {self._wirepath}")
        except _ftplib.error_perm as error:
            if _reply_code(error) in _UNSUPPORTED_REPLIES:
                raise NotImplementedError(
                    f"SITE CHMOD not supported by this server ({error})"
                ) from error
            translated = self._translate(error)
            if isinstance(translated, FileNotFoundError):
                raise translated from error
            raise NotImplementedError(
                f"SITE CHMOD refused by the server ({error})"
            ) from error
