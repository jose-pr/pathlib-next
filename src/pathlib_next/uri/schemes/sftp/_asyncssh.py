from __future__ import annotations

import asyncio as _asyncio
import collections as _collections
import concurrent.futures as _futures
import errno as _errno
import functools as _functools
import io as _io
import os as _os
import stat as _stat
import sys as _sys
import threading as _thread
import typing as _ty

import asyncssh as _asyncssh
import asyncssh.packet as _packet

from ... import Source
from .... import utils as _utils
from ....utils.stat import FileStat
from . import _checkfile, _errors
from ._sshconfig import _DEFAULT_SSH_CONFIG, _check_host, _check_proxy_user

# --- shared background event loop -------------------------------------
# asyncssh is asyncio-only end to end (connect(), every SFTPClient method,
# every SFTPClientFile method are coroutines) but SftpPath's public API is
# sync. One shared loop running in a daemon background thread, started
# lazily on first use, lets many independent SftpPath calls -- from any
# calling thread -- share one persistent connection instead of needing a
# loop (and a fresh asyncio.run()-per-call connection) per caller.
#
# Rejected alternatives (recorded so they aren't re-suggested):
#   - asyncio.run() per call: creates/destroys a loop every call, can't
#     hold a persistent connection open.
#   - a loop per calling thread: asyncssh connections aren't safe to share
#     across loops; just reimplements paramiko's thread-keyed cache with
#     extra steps and loses the one simplification available here (a
#     single connection can already serve concurrent callers from any
#     thread once it's not tied to the calling thread's own loop).

_loop: "_asyncio.AbstractEventLoop | None" = None
_loop_thread: "_thread.Thread | None" = None
_loop_pid: "int | None" = None
_loop_lock = _thread.Lock()

#: Default wall-clock bound, in seconds, on one SFTP request (a stat, an
#: open, a connect) run through `_run()`. Whole-tree operations and
#: streaming file reads/writes are not bounded by it.
_DEFAULT_TIMEOUT = 60.0


def _new_loop() -> "_asyncio.AbstractEventLoop":
    if _sys.platform == "win32":
        # The Windows default (WindowsProactorEventLoopPolicy, since
        # 3.8) is needed for subprocess pipe support, which this bridge
        # never uses (plain TCP SSH/SFTP client connections only) --
        # ProactorEventLoop's pipe transports have a known, benign-but
        # -noisy quirk where a not-yet-GC'd transport logs "Exception
        # ignored in: _ProactorBasePipeTransport.__del__" if garbage
        # collected slightly after loop shutdown, even when every
        # connection was closed and awaited correctly. SelectorEventLoop
        # doesn't have this wart and works fine for our use case.
        return _asyncio.SelectorEventLoop()
    return _asyncio.new_event_loop()


def _ensure_loop() -> "_asyncio.AbstractEventLoop":
    global _loop, _loop_thread, _loop_pid
    pid = _os.getpid()
    with _loop_lock:
        if _loop is not None and _loop_pid == pid:
            return _loop
        # First call, or a fork()'d child (Linux multiprocessing default)
        # that inherited a dead loop thread and unusable cached
        # connections -- paramiko's per-thread connections have the same
        # class of problem today, this is parity not regression, but the
        # shared singleton here makes it easier to hit.
        loop = _new_loop()
        thread = _thread.Thread(
            target=loop.run_forever, name="pathlib_next-asyncssh-loop", daemon=True
        )
        thread.start()
        _loop, _loop_thread, _loop_pid = loop, thread, pid
        # Any cached connection entries were created on the old (now dead,
        # in a fork()'d child) loop -- can't cleanly close them through a
        # loop that no longer runs, so just drop the references. Only
        # fires on a genuine PID change, never on the common lazy-first
        # -call path (cache is already empty then).
        _CACHE.reset()
        return loop


def _on_loop_thread() -> bool:
    thread = _loop_thread
    return thread is not None and thread is _thread.current_thread()


_UNSET_TIMEOUT: _ty.Any = object()

#: Longest, in seconds, `_run()` waits for a coroutine it has cancelled to
#: finish unwinding (its cleanup requests included) before it raises anyway.
_CANCEL_GRACE = 5.0


def _settle(outcome: "_futures.Future", task: "_asyncio.Task") -> None:
    """Hand a finished bridge-loop task's outcome to the thread waiting on
    `outcome`."""
    if task.cancelled():
        outcome.cancel()
    elif task.exception() is not None:
        outcome.set_exception(task.exception())
    else:
        outcome.set_result(task.result())


def _run(coro, timeout: "float | None" = _UNSET_TIMEOUT):
    """Run `coro` on the bridge loop and block the calling thread for its
    result.

    `timeout` (default `_DEFAULT_TIMEOUT`, read at call time) is meant for a
    single request -- without one, a half-dead TCP connection or a server
    that never answers blocks the caller forever. Pass `None` for anything
    whose duration grows with the data (tree operations, streaming
    reads/writes). On timeout the builtin `TimeoutError` is raised on every
    Python version.

    Whatever stops the wait before `coro` is done -- the timeout, or an
    exception delivered to the calling thread such as `KeyboardInterrupt` --
    cancels `coro` on the loop and waits (up to `_CANCEL_GRACE` seconds) for
    it to finish unwinding, so a call that has raised has stopped.

    Raises `RuntimeError` immediately when called on the bridge-loop thread
    itself (a sync `Path` method inside a coroutine or callback running
    there): blocking would deadlock the loop the coroutine needs. Also
    raised once the interpreter is finalizing: the daemon loop thread can no
    longer run, so waiting would only hang shutdown (for `timeout`, or for
    ever without one) -- e.g. a file left open at module scope.
    """
    if timeout is _UNSET_TIMEOUT:
        timeout = _DEFAULT_TIMEOUT
    if _sys.is_finalizing():
        if _asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError(
            "SFTP request made while the interpreter is shutting down -- the "
            "asyncssh bridge loop can no longer run"
        )
    loop = _ensure_loop()
    if _on_loop_thread():
        if _asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError(
            "synchronous SFTP call made on the asyncssh bridge-loop thread -- "
            "it would deadlock the loop; await the asyncssh client directly "
            "or run the call in a worker thread (asyncio.to_thread)"
        )
    outcome: "_futures.Future" = _futures.Future()
    started: "list[_asyncio.Task]" = []

    def start() -> None:
        try:
            task = loop.create_task(coro)
        except Exception as error:
            outcome.set_exception(error)
            return
        started.append(task)
        task.add_done_callback(lambda done: _settle(outcome, done))

    loop.call_soon_threadsafe(start)
    try:
        return outcome.result(timeout)
    except BaseException as error:
        if outcome.done():
            raise
        # Queued behind start(), so the task exists by the time it runs.
        loop.call_soon_threadsafe(lambda: started[0].cancel())
        try:
            outcome.exception(_CANCEL_GRACE)
        except (_futures.CancelledError, _futures.TimeoutError):
            pass
        if isinstance(error, _futures.TimeoutError):
            if (
                outcome.done()
                and not outcome.cancelled()
                and outcome.exception() is None
            ):
                # Finished between the timeout and the cancel: keep the result.
                return outcome.result()
            raise TimeoutError(
                f"SFTP request did not complete within {timeout} seconds"
            ) from None
        raise


# --- error translation ---------------------------------------------------
# asyncssh's exceptions do NOT subclass OSError (SFTPError -> asyncssh.Error ->
# Exception; a lost connection, a refused login or an untrusted host key are
# DisconnectErrors), so every call translates them. A server below SFTP v5
# has no EEXIST or ENOTEMPTY status: it answers a generic failure, which
# `SftpPath._open()`/`_mkdir()` disambiguate by consulting the entry, so
# every failure must surface as *some* OSError subclass for that handler to
# fire. Nothing is chained to the library exception (see `_errors`).


def _translate(
    error: "_asyncssh.SFTPError",
    filename: "str | None" = None,
    filename2: "str | None" = None,
) -> Exception:
    """The pathlib exception for an asyncssh `SFTPError`, built as pathlib
    builds it -- `(errno, strerror, filename[, filename2])` -- so `errno`
    and `filename` are set, as they are on the paramiko backend."""
    if isinstance(error, (_asyncssh.SFTPConnectionLost, _asyncssh.SFTPNoConnection)):
        return _errors.lost()
    if isinstance(error, (_asyncssh.SFTPNoSuchFile, _asyncssh.SFTPNoSuchPath)):
        cls, code = FileNotFoundError, _errno.ENOENT
    elif isinstance(error, _asyncssh.SFTPFileAlreadyExists):
        cls, code = FileExistsError, _errno.EEXIST
    elif isinstance(error, _asyncssh.SFTPDirNotEmpty):
        cls, code = OSError, _errno.ENOTEMPTY
    elif isinstance(error, _asyncssh.SFTPPermissionDenied):
        cls, code = PermissionError, _errno.EACCES
    elif isinstance(error, _asyncssh.SFTPOpUnsupported):
        return NotImplementedError(str(error))
    else:
        return _errors.bare_failure(str(error), filename, filename2)
    return _errors.os_error(
        cls, code, str(error) or _os.strerror(code), filename, filename2
    )


def _transport_error(
    error: "_asyncssh.Error", source: "Source | None" = None
) -> Exception:
    """The `OSError` for an asyncssh failure of the connection, the login or
    the host key; the same classes the paramiko backend raises."""
    if isinstance(error, _asyncssh.HostKeyNotVerifiable):
        return _errors.host_key_refused(source)
    if isinstance(error, _asyncssh.PermissionDenied):
        return _errors.login_refused(source)
    if isinstance(error, _asyncssh.ConnectionLost):
        return _errors.lost()
    if isinstance(error, (_asyncssh.DisconnectError, _asyncssh.ChannelOpenError)):
        return _errors.aborted(error)
    return OSError(_errno.EIO, f"SFTP request failed ({type(error).__name__})")


def _library_error(
    error: "_asyncssh.Error",
    filename: "str | None" = None,
    filename2: "str | None" = None,
    source: "Source | None" = None,
) -> Exception:
    if isinstance(error, _asyncssh.SFTPError):
        return _translate(error, filename, filename2)
    return _transport_error(error, source)


def _path_error(cls, code: int, path) -> OSError:
    return cls(code, _os.strerror(code), str(path))


#: `_SyncSftpClient` methods whose second path argument is `filename2`.
_TWO_PATH_METHODS = frozenset({"rename", "posix_rename", "symlink", "link"})


def _reraise_sftp_errors(fn):
    two_paths = fn.__name__ in _TWO_PATH_METHODS

    @_functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _asyncssh.Error as error:
            # `(self, path, ...)` for a client method; a file method's
            # arguments (a size, a buffer) name no path.
            filename = args[1] if len(args) > 1 and isinstance(args[1], str) else None
            filename2 = (
                args[2]
                if two_paths and len(args) > 2 and isinstance(args[2], str)
                else None
            )
            raise _library_error(error, filename, filename2) from None

    return wrapper


# --- stat adapter ----------------------------------------------------------
# asyncssh's SFTPAttrs has NO st_-prefixed fields at all (verified:
# hasattr(attrs, 'st_mode') is False) -- FileStat.from_stat() copies slots
# via getattr(stat, prop, 0), so feeding it a raw SFTPAttrs produces an
# all-zeros FileStat with NO error: st_mode=0 means exists()->True but
# is_dir()/is_file()->False for everything. Silent wrongness, not a crash.

_FILEXFER_TYPE_TO_S_IF = {
    _asyncssh.FILEXFER_TYPE_REGULAR: _stat.S_IFREG,
    _asyncssh.FILEXFER_TYPE_DIRECTORY: _stat.S_IFDIR,
    _asyncssh.FILEXFER_TYPE_SYMLINK: _stat.S_IFLNK,
    _asyncssh.FILEXFER_TYPE_SOCKET: _stat.S_IFSOCK,
    _asyncssh.FILEXFER_TYPE_CHAR_DEVICE: _stat.S_IFCHR,
    _asyncssh.FILEXFER_TYPE_BLOCK_DEVICE: _stat.S_IFBLK,
    _asyncssh.FILEXFER_TYPE_FIFO: _stat.S_IFIFO,
}


def _numeric_id(number: "int | None", name: "str | None") -> "int | None":
    if number is not None:
        return number
    return int(name) if name and name.isdigit() else None


class _StatAdapter:
    """Adapts an asyncssh `SFTPAttrs` to the `st_*`-shaped interface
    `FileStat.from_stat()` expects. Verified empirically (in-process
    asyncssh server, both v3 and a v6-requested-but-v3-negotiated
    session): `.permissions` already carries the combined `S_IFMT` type
    bits + permission bits in practice (e.g. `0o100666` for a regular
    file), so `S_ISDIR()`/`S_ISREG()` on it alone already work. Per the
    SFTP spec, a true v4+ server may instead report only the bare
    permission bits in `.permissions` and put the type in `.type` --
    handled defensively below (combine only when `.permissions` lacks
    `S_IFMT` bits) since that path isn't reachable against any server
    available to test against (asyncssh's own bundled `SFTPServer` stays
    at v3 regardless of the version requested; real-world OpenSSH is v3
    -only too)."""

    __slots__ = ("_attrs", "filename")

    def __init__(
        self, attrs: "_asyncssh.SFTPAttrs", filename: "str | bytes | None" = None
    ):
        self._attrs = attrs
        self.filename = filename

    @property
    def st_mode(self) -> int:
        attrs = self._attrs
        perms = attrs.permissions or 0
        if _stat.S_IFMT(perms):
            return perms
        type_bits = _FILEXFER_TYPE_TO_S_IF.get(attrs.type, 0)
        return perms | type_bits

    @property
    def st_nlink(self) -> int:
        return self._attrs.nlink or 1

    # An SFTP v4 server names the owner and the group ("owner"/"group") and
    # leaves uid/gid out: a numeric name is the id, any other leaves it 0 here
    # and `uid_known`/`gid_known` False, so `SftpPath._chown()` never sends it.
    @property
    def st_uid(self) -> int:
        return _numeric_id(self._attrs.uid, self._attrs.owner) or 0

    @property
    def st_gid(self) -> int:
        return _numeric_id(self._attrs.gid, self._attrs.group) or 0

    @property
    def uid_known(self) -> bool:
        return _numeric_id(self._attrs.uid, self._attrs.owner) is not None

    @property
    def gid_known(self) -> bool:
        return _numeric_id(self._attrs.gid, self._attrs.group) is not None

    @property
    def st_size(self) -> int:
        return self._attrs.size or 0

    @property
    def st_atime(self):
        return self._attrs.atime or 0

    @property
    def st_mtime(self):
        return self._attrs.mtime or 0

    @property
    def st_ctime(self):
        return self._attrs.ctime or 0


# --- sync file wrapper -----------------------------------------------------


class _SyncSftpFile(_io.RawIOBase):
    """Wraps an asyncssh `SFTPClientFile` (every method a coroutine,
    including read/write/seek/close) as a sync binary file-like object --
    the `BinaryOpen` protocol contract (`protocols/io.py`) expects `_open()`
    to hand back a genuine `io.IOBase`: `open()` in text mode wraps it in
    `io.TextIOWrapper`, which needs `flush()`/`readable()`/etc., not just
    `read()`/`write()` -- a bare duck-typed object without a real `io.*`
    base class raises `AttributeError: ... no attribute 'flush'` the first
    time a caller does `read_text()`/`write_text()`. Subclassing
    `io.RawIOBase` gets `flush()`, `fileno()`, `isatty()`, the context
    manager protocol, and `closed`-state bookkeeping for free -- only the
    genuinely backend-specific methods below need overriding. Construction
    always passes `encoding=None` (see `_SyncSftpClient.open`): asyncssh
    defaults to TEXT mode (`encoding='utf-8'`), unlike paramiko whose SFTP
    files are always binary -- a text-mode stream here would return `str`
    from every read and blow up every downstream `read_bytes()` caller."""

    def __init__(
        self,
        afile: "_asyncssh.SFTPClientFile",
        timeout: "float | None" = _UNSET_TIMEOUT,
        mode: str = "r+",
    ):
        super().__init__()
        self._afile = afile
        self._timeout = timeout
        self._readable = "r" in mode or "+" in mode
        self._writable = any(kind in mode for kind in "wxa+")

    # read()/write() carry whole payloads (read_bytes() is one read(-1)), so
    # their duration grows with the data: no wall-clock bound.
    @_reraise_sftp_errors
    def read(self, size: int = -1) -> bytes:
        return _run(self._afile.read(size), None)

    @_reraise_sftp_errors
    def readall(self) -> bytes:
        # One request stream to EOF (asyncssh parallelizes it), instead of
        # RawIOBase's loop of DEFAULT_BUFFER_SIZE reads.
        return _run(self._afile.read(-1), None)

    @_reraise_sftp_errors
    def readinto(self, buffer) -> int:
        view = memoryview(buffer).cast("B")
        data = _run(self._afile.read(len(view)), None)
        view[: len(data)] = data
        return len(data)

    @_reraise_sftp_errors
    def write(self, data: bytes) -> int:
        return _run(self._afile.write(bytes(data)), None)

    @_reraise_sftp_errors
    def truncate(self, size: "int | None" = None) -> int:
        if size is None:
            size = self.tell()
        _run(self._afile.truncate(size), self._timeout)
        return size

    @_reraise_sftp_errors
    def seek(self, offset: int, whence: int = 0) -> int:
        return _run(self._afile.seek(offset, whence), self._timeout)

    @_reraise_sftp_errors
    def tell(self) -> int:
        return _run(self._afile.tell(), self._timeout)

    def close(self) -> None:
        if self.closed:
            return
        try:
            if _sys.is_finalizing():
                # The loop thread cannot run any more; the server releases
                # the handle when the connection drops.
                pass
            elif _on_loop_thread():
                # A finalizer can run here; closing must not block the loop.
                _loop.create_task(self._afile.close())
            else:
                try:
                    _run(self._afile.close(), self._timeout)
                except _asyncssh.Error as error:
                    raise _library_error(error) from None
        finally:
            super().close()

    def readable(self) -> bool:
        return self._readable

    def writable(self) -> bool:
        return self._writable

    def seekable(self) -> bool:
        return True


#: Buffer size for the `io.Buffered*` wrapper `_SyncSftpClient.open()`
#: returns: each refill is one SFTP read request.
_BUFFER_SIZE = 64 * 1024


# --- sync client wrapper ----------------------------------------------------


async def _aopen(aclient: "_asyncssh.SFTPClient", path: str, mode: str):
    return await aclient.open(path, mode, encoding=None)


#: `check-file-handle` as asyncssh keys an extended request.
_CHECK_FILE_REQUEST = _checkfile.EXTENSION.encode("ascii")


async def _acheck_file(
    aclient: "_asyncssh.SFTPClient", handle: bytes, algorithm: str
) -> bytes:
    # asyncssh has no public API for an arbitrary extended request: this is
    # the `_make_request()` primitive its own statvfs@openssh.com and
    # limits@openssh.com requests use. It rejects any reply type the
    # handler's `_return_types` table does not name for the request as
    # SFTPBadMessage, so the extension is added to that table -- on this
    # connection's handler only: the class-level table is shared with
    # asyncssh's SFTP server.
    handler = aclient._handler
    if _CHECK_FILE_REQUEST not in handler._return_types:
        handler._return_types = {
            **handler._return_types,
            _CHECK_FILE_REQUEST: _asyncssh.FXP_EXTENDED_REPLY,
        }
    packet = await handler._make_request(
        _CHECK_FILE_REQUEST,
        _packet.String(handle),
        _packet.String(algorithm),  # hash-algorithm-list: just the one wanted
        _packet.UInt64(_checkfile.START_OFFSET),
        _packet.UInt64(_checkfile.LENGTH),
        _packet.UInt32(_checkfile.BLOCK_SIZE),
    )
    return packet.get_remaining_payload()


class _SyncSftpClient:
    """Sync wrapper around an asyncssh `SFTPClient`, exposing the same
    method names paramiko's `SFTPClient` uses (`stat`/`lstat`/
    `listdir_attr`/`open`/`mkdir`/`chmod`/`chown`/`remove`/`rmdir`/
    `rename`/`symlink`/`readlink`/`link`) -- `SftpPath` calls
    `self._sftpclient.X()`
    directly with no per-backend branching, so matching that shape here is
    what makes everything above "just add one more mirrored method" rather
    than new plumbing in `SftpPath` itself.

    Weakly referenceable: per-connection records (a refused
    `check-file-handle`, a missing `posix-rename@openssh.com`) are keyed on
    this object, and without `__weakref__` they are silently never kept."""

    __slots__ = ("_aclient", "_timeout", "__weakref__")

    def __init__(
        self,
        aclient: "_asyncssh.SFTPClient",
        timeout: "float | None" = _UNSET_TIMEOUT,
    ):
        self._aclient = aclient
        self._timeout = timeout

    def _run(self, coro):
        return _run(coro, self._timeout)

    @_reraise_sftp_errors
    def stat(self, path: str) -> _StatAdapter:
        return _StatAdapter(self._run(self._aclient.stat(path)))

    @_reraise_sftp_errors
    def lstat(self, path: str) -> _StatAdapter:
        return _StatAdapter(self._run(self._aclient.lstat(path)))

    @_reraise_sftp_errors
    def listdir_attr(self, path: str) -> "list[_StatAdapter]":
        # asyncssh has no listdir_attr() of its own -- readdir() returns
        # SFTPName(filename, longname, attrs), the same conceptual shape.
        names = self._run(self._aclient.readdir(path))
        return [
            _StatAdapter(name.attrs, filename=name.filename)
            for name in names
            if name.filename not in (".", "..")
        ]

    @_reraise_sftp_errors
    def open(self, path: str, mode: str = "r", buffering: int = -1) -> _io.IOBase:
        # aclient.open() is `@async_context_manager`-decorated -- calling it
        # returns a custom awaitable, not a plain coroutine object, which
        # asyncio.run_coroutine_threadsafe() rejects outright ("A coroutine
        # object is required"). Wrapping the `await` in a real `async def`
        # helper produces a genuine coroutine object that IS accepted.
        afile = self._run(_aopen(self._aclient, path, mode))
        raw = _SyncSftpFile(afile, self._timeout, mode)
        if buffering == 0:
            return raw
        # Buffered like paramiko's files and a local open(): a raw handle
        # made readline() one round trip per byte.
        size = buffering if buffering > 1 else _BUFFER_SIZE
        if "+" in mode:
            return _io.BufferedRandom(raw, size)
        if raw.readable():
            return _io.BufferedReader(raw, size)
        return _io.BufferedWriter(raw, size)

    @_reraise_sftp_errors
    def mkdir(self, path: str, mode: "int | None" = None) -> None:
        attrs = (
            _asyncssh.SFTPAttrs(permissions=mode)
            if mode is not None
            else _asyncssh.SFTPAttrs()
        )
        self._run(self._aclient.mkdir(path, attrs))

    @_reraise_sftp_errors
    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        self._run(self._aclient.chmod(path, mode, follow_symlinks=follow_symlinks))

    @_reraise_sftp_errors
    def chown(self, path: str, uid: int, gid: int) -> None:
        # asyncssh takes uid/gid as keywords on setstat and sends them as
        # SFTPv3's paired UIDGID attribute -- both values always go on the
        # wire together, which is why SftpPath._chown() reads the current
        # owner for whichever field the caller left as "unchanged".
        self._run(self._aclient.chown(path, uid, gid))

    @_reraise_sftp_errors
    def remove(self, path: str) -> None:
        self._run(self._aclient.remove(path))

    @_reraise_sftp_errors
    def rmdir(self, path: str) -> None:
        self._run(self._aclient.rmdir(path))

    @_reraise_sftp_errors
    def rename(self, oldpath: str, newpath: str) -> None:
        self._run(self._aclient.rename(oldpath, newpath))

    @_reraise_sftp_errors
    def posix_rename(self, oldpath: str, newpath: str) -> None:
        # Replaces newpath. Raises NotImplementedError (SFTPOpUnsupported,
        # decided locally) when the server did not advertise
        # posix-rename@openssh.com and the protocol is below v5.
        self._run(self._aclient.posix_rename(oldpath, newpath))

    @_reraise_sftp_errors
    def symlink(self, source: str, dest: str) -> None:
        # asyncssh's docstring confirms it auto-corrects for OpenSSH's
        # well-known swapped wire argument order internally -- the natural
        # "create dest pointing at source" call is already correct as-is.
        self._run(self._aclient.symlink(source, dest))

    @_reraise_sftp_errors
    def readlink(self, path: str) -> str:
        return self._run(self._aclient.readlink(path))

    @_reraise_sftp_errors
    def link(self, source: str, dest: str) -> None:
        # No typed exception is guaranteed for "server doesn't support this
        # extension" (asyncssh's docstring says only "SFTPError if the
        # server doesn't support this extension or returns an error") --
        # _reraise_sftp_errors still maps whatever comes back to some
        # OSError subclass; SftpPath.hardlink_to() is responsible for the
        # NotImplementedError fallback policy, not this wrapper.
        self._run(self._aclient.link(source, dest))

    @_reraise_sftp_errors
    def check_file(self, file: "_SyncSftpFile", algorithm: str) -> bytes:
        """The `check-file-handle` reply payload for `file`, an unbuffered
        handle from `open()`. The server hashes the whole file before it
        answers, so the duration grows with the data: no wall-clock bound.
        A reply of a type the request does not have is
        `NotImplementedError`, as an unsupported operation is."""
        try:
            return _run(
                _acheck_file(self._aclient, file._afile.handle, algorithm), None
            )
        except _asyncssh.SFTPBadMessage:
            raise NotImplementedError(
                f"{_checkfile.EXTENSION}: unexpected reply type"
            ) from None


# --- connection cache --------------------------------------------------
# Keyed by (backend, source) only -- no thread_id dimension, unlike
# paramiko's SftpBackend (whose client is bound to the thread that owns
# its socket-reading loop). One shared asyncio loop means a single
# SSHClientConnection + SFTPClient can serve concurrent calls from any
# calling thread.
#
# Not `utils.LRU`: closing an evicted entry has to run on the bridge loop,
# and concurrent misses on one key must wait for a single connection instead
# of each opening (and orphaning) their own.


class _ConnectionEntry(_ty.NamedTuple):
    conn: "_asyncssh.SSHClientConnection"
    client: "_SyncSftpClient"


async def _aclose_entry(entry: "_ConnectionEntry") -> None:
    try:
        entry.client._aclient.exit()
        await entry.client._aclient.wait_closed()
    except Exception:
        pass
    entry.conn.close()
    try:
        await entry.conn.wait_closed()
    except Exception:
        pass


def _entry_is_alive(entry: "_ConnectionEntry") -> bool:
    if entry.conn.is_closed():
        return False
    # The SFTP channel can close while the SSH connection stays up (server
    # ChannelTimeout, sftp-server crash); asyncssh clears the handler's
    # writer when it does.
    handler = getattr(entry.client._aclient, "_handler", None)
    return handler is None or getattr(handler, "_writer", True) is not None


def _close_entries(entries: "_ty.Iterable[_ConnectionEntry]") -> None:
    for entry in entries:
        try:
            _run(_aclose_entry(entry))
        except Exception:
            pass


class _ConnectionCache:
    __slots__ = ("_entries", "_pending", "_lock", "maxsize")

    def __init__(self, maxsize: int = 128):
        self._entries: "_collections.OrderedDict[tuple, _ConnectionEntry]" = (
            _collections.OrderedDict()
        )
        self._pending: "dict[tuple, _futures.Future]" = {}
        self._lock = _thread.Lock()
        self.maxsize = maxsize

    def get_or_create(
        self, key, factory: "_ty.Callable[[], _ConnectionEntry]"
    ) -> _ConnectionEntry:
        while True:
            stale = None
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None:
                    if _entry_is_alive(entry):
                        self._entries.move_to_end(key)
                        return entry
                    stale = self._entries.pop(key)
                pending = self._pending.get(key)
                owner = pending is None
                if owner:
                    pending = self._pending[key] = _futures.Future()
            if stale is not None:
                _close_entries([stale])
            if owner:
                break
            # Another thread is connecting this key: share its connection
            # (or its failure) instead of opening a second one.
            pending.result()

        try:
            entry = factory()
        except BaseException as error:
            with self._lock:
                self._pending.pop(key, None)
            pending.set_exception(error)
            raise
        evicted = []
        with self._lock:
            self._entries[key] = entry
            self._pending.pop(key, None)
            while len(self._entries) > self.maxsize:
                evicted.append(self._entries.popitem(last=False)[1])
        pending.set_result(None)
        _close_entries(evicted)
        return entry

    def invalidate(self, key) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            _close_entries([entry])

    def close_backend(self, backend) -> None:
        with self._lock:
            keys = [key for key in self._entries if key[0] is backend]
            entries = [self._entries.pop(key) for key in keys]
        _close_entries(entries)

    def reset(self) -> None:
        # Used only by _ensure_loop() on a detected PID change (fork()'d
        # child) -- the old loop is dead by then, so entries are just
        # dropped, not actively closed (nothing left to run the close
        # coroutine on).
        with self._lock:
            self._entries.clear()
            self._pending.clear()


_CACHE = _ConnectionCache()


def _check_user_for_proxy(host: str, user: str, kwargs: "dict[str, _ty.Any]") -> None:
    """Refuse `user` when asyncssh would put it into a ProxyCommand's argv.

    asyncssh expands the ssh_config `ProxyCommand` tokens (`%r` is the user)
    and splits the result into arguments afterwards, so a user holding white
    space or a leading `-` adds arguments. A server accepts such a user, so
    only the one the configuration for this host would pass on is refused:
    the command is resolved with this user and with a safe one, and a
    difference means the user is in it.
    """
    try:
        _check_proxy_user(user)
        return
    except ValueError as refusal:
        refused = refusal
    options = {"host": host, "known_hosts": None, "client_keys": None}
    for key in ("config", "port"):
        if key in kwargs:
            options[key] = kwargs[key]
    resolve = _functools.partial(_asyncssh.SSHClientConnectionOptions, **options)
    if resolve(username=user).proxy_command != resolve(username="user").proxy_command:
        raise refused from None


async def _aconnect(
    source: "Source",
    connect_opts: "_ty.Mapping[str, _ty.Any] | None" = None,
    sftp_version: int = 4,
    timeout: "float | None" = _UNSET_TIMEOUT,
) -> _ConnectionEntry:
    _check_host(source.host)
    user, password = source.parsed_userinfo()
    # No `known_hosts` default: asyncssh then verifies the server key against
    # ~/.ssh/known_hosts and the ssh_config's UserKnownHostsFile.
    kwargs: "dict[str, _ty.Any]" = {}
    if connect_opts:
        kwargs.update(connect_opts)
    if source.port:
        # Only a port the URI names: an explicit one outranks the ssh_config
        # `Port`, which asyncssh applies when none is passed.
        kwargs["port"] = source.port
    if user:
        _check_user_for_proxy(str(source.host), user, kwargs)
        kwargs["username"] = user
    if password:
        kwargs["password"] = password
    conn = await _asyncssh.connect(str(source.host), **kwargs)
    try:
        # asyncssh currently supports SFTP protocol versions 3 and 4 here --
        # request the configured maximum and let the server negotiate down
        # (real-world OpenSSH still stays at v3).
        # Names the server sends that are not UTF-8 come back with their
        # bytes as lone surrogates, and go back out as the same bytes.
        aclient = await conn.start_sftp_client(
            sftp_version=sftp_version, path_errors="surrogateescape"
        )
    except BaseException:
        conn.close()
        raise
    return _ConnectionEntry(conn, _SyncSftpClient(aclient, timeout))


class AsyncsshSftpBackend(_checkfile.CheckFileSftpBackend):
    """`sftp:` backend using `asyncssh` instead of paramiko. Selected
    automatically when `asyncssh` is importable (see backend selection in
    `sftp/__init__.py`), or explicitly via `backend=AsyncsshSftpBackend()`
    /`PATHLIB_NEXT_SFTP_BACKEND=asyncssh`. Connections are cached per
    `(self, source)` (see `_ConnectionCache` above) and served through a
    single shared background asyncio loop (see `_run` above) -- not
    fork-safe (a `fork()`ed child inherits a dead loop thread; detected via
    stored PID, loop+cache lazily recreated when `os.getpid()` changes).
    `close()` closes every connection the backend opened.

    Host keys are verified by default: asyncssh checks the server key
    against `~/.ssh/known_hosts` and the ssh_config's `UserKnownHostsFile`,
    and an unknown or changed key raises `SftpHostKeyError`. **Opt-out**, in code
    only: `AsyncsshSftpBackend(connect_opts={"known_hosts": None})` accepts
    any server key -- a network man-in-the-middle then receives the URI
    password. Any other asyncssh `known_hosts` value (a file, a list of
    keys) is passed through as given.

    `timeout` (default `_DEFAULT_TIMEOUT`, 60 s) bounds a single request --
    connect, stat, open, mkdir, rename, ... -- and a timed-out request is
    cancelled and raises `TimeoutError`. Recursive `copy()`/`rm()`,
    streaming file reads/writes (`read_bytes()`, `write_bytes()`, chunked
    copies) and a native `checksum()` (the server hashes the whole file)
    have no wall-clock bound; asyncssh's own `connect_timeout`,
    `login_timeout` and `keepalive_interval` connect options detect a dead
    peer there. `timeout=None` disables the per-request bound too."""

    __slots__ = ("connect_opts", "max_concurrency", "sftp_version", "timeout")

    #: asyncssh's chmod() takes follow_symlinks natively.
    supports_lchmod = True
    #: Recursive `copy()` and `rm()` have a concurrent implementation here.
    supports_tree = True
    #: asyncssh's SFTPClient.link() exists (SFTPv3 has no core hard-link
    #: op, but this works via the hardlink@openssh.com extension against
    #: real-world OpenSSH v3 servers, or the standard opcode against v5/v6).
    supports_hardlink = True

    #: Default bound on concurrent SFTP requests during a recursive copy/rm.
    #: 16 (raised from 8 in 0.8.3): a 128-file loopback sweep of mc in
    #: {1,2,4,8,16} (median of 3, 3.14) showed recursive copy improving
    #: monotonically with concurrency -- mc=1 -> mc=8 ~1.13x, mc=8 -> mc=16 a
    #: further ~3% -- with recursive rm flat (spread within run-to-run noise) and
    #: mc=16 fastest-or-tied for both. 16 stays well inside asyncssh's SFTP
    #: request window, so the extra in-flight requests carry no overload risk.
    #: NOTE: loopback evidence only (no per-op network latency); a high-latency
    #: remote link may favour even higher concurrency, but 16 is a safe, modest
    #: default. Override per-backend via ``max_concurrency=``.
    DEFAULT_MAX_CONCURRENCY = 16

    def __init__(
        self,
        connect_opts: "dict[str, _ty.Any] | None" = None,
        *,
        max_concurrency: "int | None" = None,
        sftp_version: int = 4,
        ssh_config=_DEFAULT_SSH_CONFIG,
        timeout: "float | None" = _DEFAULT_TIMEOUT,
    ):
        if max_concurrency is None:
            max_concurrency = self.DEFAULT_MAX_CONCURRENCY
        self.connect_opts = {} if connect_opts is None else dict(connect_opts)
        if "config" not in self.connect_opts:
            if ssh_config is None:
                self.connect_opts["config"] = None
            elif ssh_config is not _DEFAULT_SSH_CONFIG:
                self.connect_opts["config"] = ssh_config
        self.max_concurrency = max_concurrency
        self.sftp_version = sftp_version
        self.timeout = timeout

    def client(self, source: "Source") -> _SyncSftpClient:
        try:
            entry = _CACHE.get_or_create(
                (self, source),
                lambda: _run(
                    _aconnect(
                        source,
                        connect_opts=self.connect_opts,
                        sftp_version=self.sftp_version,
                        timeout=self.timeout,
                    ),
                    self.timeout,
                ),
            )
        except _asyncssh.Error as error:
            raise _transport_error(error, source) from None
        return entry.client

    def close(self) -> None:
        """Close every cached connection this backend opened."""
        _CACHE.close_backend(self)

    def _check_file_request(
        self, client: _SyncSftpClient, file: "_SyncSftpFile", algorithm: str
    ) -> bytes:
        # SSH_FX_OP_UNSUPPORTED and an unexpected reply type both arrive as
        # NotImplementedError, which `_checkfile.refused()` reads as a refusal.
        return client.check_file(file, algorithm)

    @classmethod
    def default(cls, ssh_config=_DEFAULT_SSH_CONFIG) -> "AsyncsshSftpBackend":
        return cls(ssh_config=ssh_config)

    # The connection is resolved on the calling thread: looking it up on the
    # bridge loop would open it through a blocking `_run()` on that loop.
    def tree_rm(self, path, *, missing_ok, on_error):
        return _run(
            _concurrent_rm(
                path,
                max_concurrency=self.max_concurrency,
                missing_ok=missing_ok,
                on_error=on_error,
                aclient=path._sftpclient._aclient,
            ),
            None,
        )

    def tree_copy(
        self,
        path,
        target,
        *,
        overwrite,
        follow_symlinks,
        preserve_metadata,
        ignore_error,
    ):
        return _run(
            _concurrent_copy(
                path,
                target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                max_concurrency=self.max_concurrency,
                ignore_error=ignore_error,
                aclient=path._sftpclient._aclient,
            ),
            None,
        )


class _TaskOwner:
    """Owns every task one recursive walk creates, so that none outlives it:
    `close()` cancels the ones still running and waits until they have all
    finished."""

    __slots__ = ("_tasks",)

    def __init__(self) -> None:
        self._tasks: "set[_asyncio.Task]" = set()

    def spawn(self, coro) -> "_asyncio.Task":
        task = _asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: "_asyncio.Task") -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            # A failure nobody awaited (a second one, behind the first that
            # was raised) is read here, or asyncio logs it as never retrieved.
            task.exception()

    async def close(self) -> None:
        pending = list(self._tasks)
        for task in pending:
            # Once: a task already unwinding from a cancel must not be cut
            # short in the cleanup requests it makes.
            task.cancel()
        interrupted = None
        while pending:
            try:
                await _asyncio.wait(pending)
            except _asyncio.CancelledError as error:
                interrupted = error
            pending = [task for task in pending if not task.done()]
        if interrupted is not None:
            raise interrupted


async def _in_thread(function, *args):
    """`asyncio.to_thread()` for a worker the caller cannot abandon: a thread
    cannot be interrupted, so a cancelled caller waits for it to return
    before it unwinds."""
    work = _asyncio.ensure_future(_asyncio.to_thread(function, *args))
    try:
        return await _asyncio.shield(work)
    except _asyncio.CancelledError:
        await _asyncio.wait([work])
        if not work.cancelled():
            work.exception()
        raise


async def _sftp_call(semaphore, make_awaitable, filename=None):
    """Run one asyncssh request inside `semaphore`, raising what the sync
    client raises for a failure of it."""
    async with semaphore:
        try:
            return await make_awaitable()
        except _asyncssh.Error as error:
            raise _library_error(error, filename) from None


async def _read_children(aclient, semaphore, current):
    """`[(child path, listed FileStat or None)]` for the entries of the
    directory `current`, in the server's order. The listing's own attributes
    come with it (they describe the entry, never what a link points at); None
    when the server did not say what kind of entry it is."""
    names = await _sftp_call(
        semaphore, lambda: aclient.readdir(current.path), current.path
    )
    children = []
    for entry in names:
        # The server chooses these names: one that is not a single component
        # inside `current` must never become a child path (see
        # `SftpPath._scandir`). The walkers bypass `_scandir`, so they filter.
        if not _utils.is_safe_child_name(entry.filename):
            continue
        attrs = getattr(entry, "attrs", None)
        listed = None
        if attrs is not None:
            listed = FileStat.from_stat(_StatAdapter(attrs))
            if not _stat.S_IFMT(listed.st_mode):
                listed = None
        children.append((current / entry.filename, listed))
    return children


async def _wait_fail_fast(tasks):
    """Wait for `tasks`, raising the first failure as soon as it happens. The
    other tasks are left to the `_TaskOwner`, which cancels them."""
    pending = set(tasks)
    while pending:
        done, pending = await _asyncio.wait(
            pending, return_when=_asyncio.FIRST_EXCEPTION
        )
        for task in done:
            task.result()


async def _concurrent_copy(
    path,
    target,
    overwrite: bool,
    follow_symlinks: bool,
    preserve_metadata: bool,
    max_concurrency: int,
    ignore_error,
    aclient=None,
):
    """Concurrent recursive child copies, bounded by max_concurrency.

    `aclient` must be resolved on the calling thread (`SftpPath.copy` does):
    looking it up here, on the bridge loop, would open the connection
    through a blocking `_run()` on the loop thread itself.

    Every task the walk creates belongs to one `_TaskOwner`, which cancels
    and awaits the ones still running before this coroutine returns or
    raises: a copy that has raised has stopped, and a destination file whose
    copy did not complete is removed. A source is opened before the
    destination it replaces is touched, and modes are applied as
    `Path.copy()` applies them (see `_copy_mode` in `path.py`).
    """
    semaphore = _asyncio.Semaphore(max(1, max_concurrency))
    # Separate from `semaphore`, which every request inside copy_file also
    # takes: holding a slot of that one for a whole file deadlocks once
    # max_concurrency files are open. This one caps files open at once
    # (two remote handles each) instead of letting every queued task open
    # its handles before the first file finishes.
    file_semaphore = _asyncio.Semaphore(max(1, max_concurrency))
    owner = _TaskOwner()
    # The setuid, setgid and sticky bits are applied only between two paths of
    # one class; a mode read from another class must not make a privileged file.
    keep_special_bits = type(path) is type(target)
    if aclient is None:
        aclient = path._sftpclient._aclient

    def sftp_call(make_awaitable, filename=None):
        return _sftp_call(semaphore, make_awaitable, filename)

    async def stat_path(current):
        stat_coro = aclient.stat if follow_symlinks else aclient.lstat
        attrs = await sftp_call(lambda: stat_coro(current.path), current.path)
        return FileStat.from_stat(_StatAdapter(attrs))

    async def exists_stat(current):
        try:
            return await stat_path(current)
        except FileNotFoundError:
            return None

    async def mkdir(current):
        # The mode `Path.mkdir()` sends; the source's is applied afterwards.
        attrs = _asyncssh.SFTPAttrs(permissions=0o777)
        await sftp_call(lambda: aclient.mkdir(current.path, attrs), current.path)

    async def unlink(current):
        await sftp_call(lambda: aclient.remove(current.path), current.path)

    async def apply_mode(current, source_stat):
        """Give `current` the permission bits `source_stat` reports, when it
        reports any (`Path.copy()`'s `_copy_mode`)."""
        if not (preserve_metadata and source_stat.mode_known and source_stat.st_mode):
            return
        mode = _stat.S_IMODE(source_stat.st_mode)
        if not keep_special_bits:
            mode &= ~(_stat.S_ISUID | _stat.S_ISGID | _stat.S_ISVTX)
        try:
            await sftp_call(lambda: aclient.chmod(current.path, mode), current.path)
        except NotImplementedError:
            pass

    async def quietly(make_awaitable, filename):
        # Cleanup of a copy that is already failing: its own failure is not
        # worth reporting over the one being raised.
        try:
            await sftp_call(make_awaitable, filename)
        except Exception:
            pass

    async def open_file(file, mode):
        """Open `file`. A cancellation that arrives with the request on the
        wire lets it finish and then undoes it (closes the handle, removes a
        file the open created): the request cannot be recalled, and an
        abandoned one leaves a handle and a zero-length file on the server."""
        request = _asyncio.ensure_future(
            sftp_call(lambda: _aopen(aclient, file.path, mode), file.path)
        )
        try:
            return await _asyncio.shield(request)
        except _asyncio.CancelledError:
            await _asyncio.wait([request])
            if not request.cancelled() and request.exception() is None:
                handle = request.result()
                await quietly(lambda: handle.close(), file.path)
                if "w" in mode:
                    await quietly(lambda: aclient.remove(file.path), file.path)
            raise

    async def copy_file(src, dst):
        existing = await exists_stat(dst)
        if existing is not None:
            if existing.is_dir():
                raise _path_error(IsADirectoryError, _errno.EISDIR, dst)
            if not overwrite:
                raise _path_error(FileExistsError, _errno.EEXIST, dst)

        async with file_semaphore:
            # Open the source before the destination is touched at all: a
            # source that cannot be read leaves an existing destination intact.
            src_file = await open_file(src, "rb")
            try:
                if existing is not None:
                    try:
                        await unlink(dst)
                    except FileNotFoundError:
                        pass
                dst_file = await open_file(dst, "wb")
                try:
                    while True:
                        chunk = await sftp_call(
                            lambda: src_file.read(1024 * 1024), src.path
                        )
                        if not chunk:
                            break
                        await sftp_call(
                            lambda chunk=chunk: dst_file.write(chunk), dst.path
                        )
                    await sftp_call(lambda: dst_file.close(), dst.path)
                except BaseException:
                    # A half-written file is not a copy of anything. Closed
                    # first: a server may refuse to remove an open file.
                    await quietly(lambda: dst_file.close(), dst.path)
                    await quietly(lambda: aclient.remove(dst.path), dst.path)
                    raise
            finally:
                # A read handle: nothing is lost if its close fails.
                await quietly(lambda: src_file.close(), src.path)

    async def copy_with_sync_fallback(src, dst):
        async with semaphore:
            await _in_thread(
                _functools.partial(
                    src.copy,
                    dst,
                    overwrite=overwrite,
                    follow_symlinks=follow_symlinks,
                    preserve_metadata=preserve_metadata,
                    recursive=True,
                    ignore_error=ignore_error,
                )
            )

    async def copy_node(src, dst, listed=None):
        # A listed entry that is not a link says what it is; a link is
        # followed (or not) by the request that stats it.
        if listed is not None and not (follow_symlinks and listed.is_symlink()):
            src_stat = listed
        else:
            src_stat = await stat_path(src)
        if src_stat.is_symlink():
            await copy_with_sync_fallback(src, dst)
            return

        if src_stat.is_dir():
            existing = await exists_stat(dst)
            if existing is not None:
                if not existing.is_dir():
                    raise _path_error(FileExistsError, _errno.EEXIST, dst)
                if not overwrite:
                    raise _path_error(FileExistsError, _errno.EEXIST, dst)
            else:
                await mkdir(dst)

            await settle(
                [
                    owner.spawn(copy_node(child, dst / child.name, child_stat))
                    for child, child_stat in await _read_children(
                        aclient, semaphore, src
                    )
                ]
            )
        elif src_stat.is_file():
            await copy_file(src, dst)
        else:
            await copy_with_sync_fallback(src, dst)
            return

        await apply_mode(dst, src_stat)

    async def settle(tasks):
        # Path.copy()'s contract: a callable is notified and the error
        # suppressed, True suppresses, False/None fail fast. A failure that
        # is raised leaves the other tasks to the owner, which cancels them.
        if not tasks:
            return
        if ignore_error:
            await _asyncio.wait(tasks)
            for task in tasks:
                try:
                    task.result()
                except Exception as error:
                    if callable(ignore_error):
                        # User code: off the loop thread, so it may call sync
                        # Path methods (which _run() back onto this loop).
                        await _in_thread(ignore_error, error)
            return
        await _wait_fail_fast(tasks)

    try:
        await settle(
            [
                owner.spawn(copy_node(child, target / child.name, child_stat))
                for child, child_stat in await _read_children(aclient, semaphore, path)
            ]
        )
        # The root last, after what is in it, like every other directory.
        await apply_mode(target, await stat_path(path))
    finally:
        await owner.close()


async def _concurrent_rm(
    path,
    *,
    max_concurrency: int,
    missing_ok: bool,
    on_error,
    aclient=None,
):
    """Native asyncssh recursive remove, bounded by max_concurrency.

    `aclient` must be resolved on the calling thread (`SftpPath.rm` does):
    see `_concurrent_copy`, which also explains the `_TaskOwner`. `on_error`
    runs in a worker thread, so it may call sync `Path` methods. Only the
    root is stat-ed: every entry below it is a file or a directory by what
    its parent's listing said.
    """
    semaphore = _asyncio.Semaphore(max(1, max_concurrency))
    owner = _TaskOwner()
    # Errors `on_error` declined, on their way up through the enclosing
    # directories: each error is offered once.
    declined: "list[Exception]" = []
    if aclient is None:
        aclient = path._sftpclient._aclient

    def sftp_call(make_awaitable, filename=None):
        return _sftp_call(semaphore, make_awaitable, filename)

    async def handled(error, current) -> bool:
        if on_error is None or any(error is seen for seen in declined):
            return False
        if await _in_thread(on_error, error, current):
            return True
        declined.append(error)
        return False

    async def stat_path(current):
        attrs = await sftp_call(lambda: aclient.lstat(current.path), current.path)
        return FileStat.from_stat(_StatAdapter(attrs))

    async def remove_file(current):
        await sftp_call(lambda: aclient.remove(current.path), current.path)

    async def remove_dir(current):
        await sftp_call(lambda: aclient.rmdir(current.path), current.path)

    async def rm_one(current, listed=None, *, root: bool = False):
        try:
            try:
                stat = listed if listed is not None else await stat_path(current)
            except FileNotFoundError:
                # `missing_ok` is about the path rm() was called on, not about
                # an entry that vanished below it.
                if root and missing_ok:
                    return
                raise
            if stat.is_dir():
                tasks = [
                    owner.spawn(rm_one(child, child_stat))
                    for child, child_stat in await _read_children(
                        aclient, semaphore, current
                    )
                ]
                if tasks:
                    await _wait_fail_fast(tasks)
                await remove_dir(current)
            else:
                await remove_file(current)
        except Exception as error:
            if not await handled(error, current):
                raise

    try:
        await rm_one(path, root=True)
    finally:
        await owner.close()
