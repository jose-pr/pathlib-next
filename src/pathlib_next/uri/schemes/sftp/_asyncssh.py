from __future__ import annotations

import asyncio as _asyncio
import errno as _errno
import functools as _functools
import io as _io
import os as _os
import stat as _stat
import sys as _sys
import threading as _thread
import typing as _ty

import asyncssh as _asyncssh

from ... import Source
from . import BaseSftpBackend

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


def _run(coro, timeout: "float | None" = _DEFAULT_TIMEOUT):
    # No timeout would block the calling thread forever on a half-dead TCP
    # connection or a server that never responds -- a generous default
    # beats an unconditional infinite wait.
    loop = _ensure_loop()
    future = _asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout)


# --- error translation ---------------------------------------------------
# asyncssh's SFTPError hierarchy does NOT subclass OSError (verified by
# introspection: MRO is SFTPError -> asyncssh.misc.Error -> Exception) --
# every call needs explicit translation, unlike paramiko's mostly-already
# -typed exceptions. Real-world OpenSSH sftp-server only ever speaks
# protocol v3, which has no EEXIST/DIR_NOT_EMPTY status code at all -- a v3
# server answers generic SFTPFailure regardless of client library for
# those specifically, so the exists()-after-failure disambiguation already
# in SftpPath._open()/_mkdir() (generic `except OSError:`) still does the
# real work there; this translation layer's job is just making sure every
# asyncssh failure surfaces as *some* OSError subclass so that generic
# handler actually fires instead of an unrelated SFTPError propagating.


def _translate(error: "_asyncssh.SFTPError") -> Exception:
    if isinstance(error, (_asyncssh.SFTPNoSuchFile, _asyncssh.SFTPNoSuchPath)):
        return FileNotFoundError(str(error))
    if isinstance(error, _asyncssh.SFTPFileAlreadyExists):
        return FileExistsError(str(error))
    if isinstance(error, _asyncssh.SFTPDirNotEmpty):
        return OSError(_errno.ENOTEMPTY, str(error))
    if isinstance(error, _asyncssh.SFTPPermissionDenied):
        return PermissionError(str(error))
    if isinstance(error, _asyncssh.SFTPOpUnsupported):
        return NotImplementedError(str(error))
    return OSError(str(error))


def _reraise_sftp_errors(fn):
    @_functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _asyncssh.SFTPError as error:
            raise _translate(error) from error

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

    def __init__(self, attrs: "_asyncssh.SFTPAttrs", filename: "str | bytes | None" = None):
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

    @property
    def st_uid(self) -> int:
        return self._attrs.uid or 0

    @property
    def st_gid(self) -> int:
        return self._attrs.gid or 0

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

    def __init__(self, afile: "_asyncssh.SFTPClientFile"):
        super().__init__()
        self._afile = afile

    @_reraise_sftp_errors
    def read(self, size: int = -1) -> bytes:
        return _run(self._afile.read(size))

    @_reraise_sftp_errors
    def write(self, data: bytes) -> int:
        return _run(self._afile.write(data))

    @_reraise_sftp_errors
    def seek(self, offset: int, whence: int = 0) -> int:
        return _run(self._afile.seek(offset, whence))

    def tell(self) -> int:
        return _run(self._afile.tell())

    def close(self) -> None:
        if not self.closed:
            _run(self._afile.close())
        super().close()

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True


# --- sync client wrapper ----------------------------------------------------


async def _aopen(aclient: "_asyncssh.SFTPClient", path: str, mode: str):
    return await aclient.open(path, mode, encoding=None)


class _SyncSftpClient:
    """Sync wrapper around an asyncssh `SFTPClient`, exposing the same
    method names paramiko's `SFTPClient` uses (`stat`/`lstat`/
    `listdir_attr`/`open`/`mkdir`/`chmod`/`remove`/`rmdir`/`rename`/
    `symlink`/`readlink`/`link`) -- `SftpPath` calls `self._sftpclient.X()`
    directly with no per-backend branching, so matching that shape here is
    what makes everything above "just add one more mirrored method" rather
    than new plumbing in `SftpPath` itself."""

    __slots__ = ("_aclient",)

    def __init__(self, aclient: "_asyncssh.SFTPClient"):
        self._aclient = aclient

    @_reraise_sftp_errors
    def stat(self, path: str) -> _StatAdapter:
        return _StatAdapter(_run(self._aclient.stat(path)))

    @_reraise_sftp_errors
    def lstat(self, path: str) -> _StatAdapter:
        return _StatAdapter(_run(self._aclient.lstat(path)))

    @_reraise_sftp_errors
    def listdir_attr(self, path: str) -> "list[_StatAdapter]":
        # asyncssh has no listdir_attr() of its own -- readdir() returns
        # SFTPName(filename, longname, attrs), the same conceptual shape.
        names = _run(self._aclient.readdir(path))
        return [
            _StatAdapter(name.attrs, filename=name.filename)
            for name in names
            if name.filename not in (".", "..")
        ]

    @_reraise_sftp_errors
    def open(self, path: str, mode: str = "r", buffering: int = -1) -> _SyncSftpFile:
        # aclient.open() is `@async_context_manager`-decorated -- calling it
        # returns a custom awaitable, not a plain coroutine object, which
        # asyncio.run_coroutine_threadsafe() rejects outright ("A coroutine
        # object is required"). Wrapping the `await` in a real `async def`
        # helper produces a genuine coroutine object that IS accepted.
        afile = _run(_aopen(self._aclient, path, mode))
        return _SyncSftpFile(afile)

    @_reraise_sftp_errors
    def mkdir(self, path: str, mode: "int | None" = None) -> None:
        attrs = _asyncssh.SFTPAttrs(permissions=mode) if mode is not None else _asyncssh.SFTPAttrs()
        _run(self._aclient.mkdir(path, attrs))

    @_reraise_sftp_errors
    def chmod(self, path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        _run(self._aclient.chmod(path, mode, follow_symlinks=follow_symlinks))

    @_reraise_sftp_errors
    def remove(self, path: str) -> None:
        _run(self._aclient.remove(path))

    @_reraise_sftp_errors
    def rmdir(self, path: str) -> None:
        _run(self._aclient.rmdir(path))

    @_reraise_sftp_errors
    def rename(self, oldpath: str, newpath: str) -> None:
        _run(self._aclient.rename(oldpath, newpath))

    @_reraise_sftp_errors
    def symlink(self, source: str, dest: str) -> None:
        # asyncssh's docstring confirms it auto-corrects for OpenSSH's
        # well-known swapped wire argument order internally -- the natural
        # "create dest pointing at source" call is already correct as-is.
        _run(self._aclient.symlink(source, dest))

    @_reraise_sftp_errors
    def readlink(self, path: str) -> str:
        target = _run(self._aclient.readlink(path))
        return target.decode() if isinstance(target, bytes) else target

    @_reraise_sftp_errors
    def link(self, source: str, dest: str) -> None:
        # No typed exception is guaranteed for "server doesn't support this
        # extension" (asyncssh's docstring says only "SFTPError if the
        # server doesn't support this extension or returns an error") --
        # _reraise_sftp_errors still maps whatever comes back to some
        # OSError subclass; SftpPath.hardlink_to() is responsible for the
        # NotImplementedError fallback policy, not this wrapper.
        _run(self._aclient.link(source, dest))


# --- connection cache --------------------------------------------------
# Keyed by (backend, source) only -- no thread_id dimension, unlike
# paramiko's SftpBackend (whose client is bound to the thread that owns
# its socket-reading loop). One shared asyncio loop means a single
# SSHClientConnection + SFTPClient can serve concurrent calls from any
# calling thread.
#
# Not `utils.LRU`: an evicted entry there is just discarded (`popitem`) and
# left to GC. Tolerable for paramiko (its own GC closes the socket, worst
# case a lingering thread) but an asyncssh connection GC'd off-loop emits
# "unclosed connection"/loop warnings and leaks the server-side session
# until TCP notices -- eviction here must actively close the connection on
# the bridge loop instead.


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


class _ConnectionCache:
    __slots__ = ("_entries", "_order", "_lock", "maxsize")

    def __init__(self, maxsize: int = 128):
        self._entries: "dict[tuple, _ConnectionEntry]" = {}
        self._order: "list[tuple]" = []
        self._lock = _thread.Lock()
        self.maxsize = maxsize

    def get_or_create(self, key, factory: "_ty.Callable[[], _ConnectionEntry]") -> _ConnectionEntry:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if not entry.conn.is_closed():
                    self._order.remove(key)
                    self._order.append(key)
                    return entry
                # stale -- fall through and recreate
                self._entries.pop(key, None)
                self._order.remove(key)
        entry = factory()
        evicted = None
        with self._lock:
            self._entries[key] = entry
            self._order.append(key)
            if len(self._order) > self.maxsize:
                evicted_key = self._order.pop(0)
                evicted = self._entries.pop(evicted_key, None)
        if evicted is not None:
            _run(_aclose_entry(evicted))
        return entry

    def invalidate(self, key) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
            if key in self._order:
                self._order.remove(key)
        if entry is not None:
            _run(_aclose_entry(entry))

    def reset(self) -> None:
        # Used only by _ensure_loop() on a detected PID change (fork()'d
        # child) -- the old loop is dead by then, so entries are just
        # dropped, not actively closed (nothing left to run the close
        # coroutine on).
        with self._lock:
            self._entries.clear()
            self._order.clear()


_CACHE = _ConnectionCache()


async def _aconnect(source: "Source") -> _ConnectionEntry:
    user, password = source.parsed_userinfo()
    kwargs: "dict[str, _ty.Any]" = {"known_hosts": None}
    if user:
        kwargs["username"] = user
    if password:
        kwargs["password"] = password
    conn = await _asyncssh.connect(str(source.host), source.port or 22, **kwargs)
    # asyncssh's client defaults to requesting v3 for compatibility --
    # request v6 explicitly; asyncssh negotiates down to whatever the
    # server actually supports (SFTPClient.version reports what was
    # granted -- real-world OpenSSH stays at v3 regardless).
    aclient = await conn.start_sftp_client(sftp_version=6)
    return _ConnectionEntry(conn, _SyncSftpClient(aclient))


class AsyncsshSftpBackend(BaseSftpBackend):
    """`sftp:` backend using `asyncssh` instead of paramiko. Selected
    automatically when `asyncssh` is importable (see backend selection in
    `sftp/__init__.py`), or explicitly via `backend=AsyncsshSftpBackend()`
    /`PATHLIB_NEXT_SFTP_BACKEND=asyncssh`. Connections are cached per
    `(self, source)` (see `_ConnectionCache` above) and served through a
    single shared background asyncio loop (see `_run` above) -- not
    fork-safe (a `fork()`ed child inherits a dead loop thread; detected via
    stored PID, loop+cache lazily recreated when `os.getpid()` changes)."""

    __slots__ = ("max_concurrency",)

    #: asyncssh's chmod() takes follow_symlinks natively.
    supports_lchmod = True
    #: asyncssh's SFTPClient.link() exists (SFTPv3 has no core hard-link
    #: op, but this works via the hardlink@openssh.com extension against
    #: real-world OpenSSH v3 servers, or the standard opcode against v5/v6).
    supports_hardlink = True

    def __init__(self, max_concurrency: int = 8):
        self.max_concurrency = max_concurrency

    def client(self, source: "Source") -> _SyncSftpClient:
        entry = _CACHE.get_or_create(
            (self, source), lambda: _run(_aconnect(source))
        )
        return entry.client

    @classmethod
    def default(cls) -> "AsyncsshSftpBackend":
        return cls()


async def _concurrent_copy(
    path,
    target,
    overwrite: bool,
    follow_symlinks: bool,
    preserve_metadata: bool,
    max_concurrency: int,
    ignore_error,
):
    """Concurrent recursive child copies, bounded by max_concurrency."""
    semaphore = _asyncio.Semaphore(max_concurrency)

    async def copy_child(child):
        async with semaphore:
            child_target = target / child.name
            await _asyncio.to_thread(
                child.copy,
                child_target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                recursive=True,
                ignore_error=ignore_error,
            )

    tasks = [_asyncio.create_task(copy_child(child)) for child in path.iterdir()]
    if not tasks:
        return

    if ignore_error is not None:
        results = await _asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                ignore_error(result)
        return

    pending = set(tasks)
    while pending:
        done, pending = await _asyncio.wait(
            pending, return_when=_asyncio.FIRST_EXCEPTION
        )
        for task in done:
            error = task.exception()
            if error is not None:
                for sibling in pending:
                    sibling.cancel()
                await _asyncio.gather(*pending, return_exceptions=True)
                raise error
