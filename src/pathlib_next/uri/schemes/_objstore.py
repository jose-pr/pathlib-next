"""What `s3:`, `gs:` and `az:` share: how a transport failure becomes an
`OSError`, which keys of a prefix a directory walk can reach, and the guard
that keeps a recursive copy and a recursive remove to the same set of keys.

A walk lists one level at a time with a delimiter, so it never visits a key
whose name has an empty, `.` or `..` segment, the subtree below a key that is
also an object, or the content of a key that ends in the delimiter. A flat
listing of the prefix holds all of them."""

from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import io as _io
import threading as _threading
import typing as _ty

from ... import utils as _utils
from ...path import _check_follow
from ...utils.stat import FileStat
from .. import UriPath as _UriPath

_state = _threading.local()

#: What a failure of the connection to the store is, as `transport_error()`
#: raises it: the request ran out of time, the endpoint could not be reached,
#: the connection broke after the store had begun to answer, or anything else
#: the HTTP client reported.
TIMEOUT, UNREACHABLE, INTERRUPTED, FAILED = (
    "timeout",
    "unreachable",
    "interrupted",
    "failed",
)

_TRANSPORT = {
    TIMEOUT: (TimeoutError, _errno.ETIMEDOUT, "request timed out"),
    UNREACHABLE: (ConnectionError, _errno.EHOSTUNREACH, "endpoint unreachable"),
    INTERRUPTED: (
        ConnectionResetError,
        _errno.ECONNRESET,
        "connection lost before the response was complete",
    ),
    FAILED: (OSError, _errno.EIO, "request failed"),
}


def transport_error(kind: str, store: str, path, error: BaseException) -> OSError:
    """The `OSError` for a transport failure of `store` ("S3", "GCS", "Azure")
    on `path`: `TimeoutError`, `ConnectionError`, `ConnectionResetError` or
    plain `OSError` by `kind`, with the path as `filename`. Only the type name
    of `error` reaches the message: an HTTP client's own text carries the
    request URL, and with it any query-string credential. Raise it
    `from None`."""
    cls, number, text = _TRANSPORT[kind]
    return cls(number, f"{store} {text} ({type(error).__name__})", str(path))


def mentions_timeout(error: BaseException) -> bool:
    """Whether `error`, or an exception it wraps (`__cause__`, `__context__`,
    an SDK's `inner_exception` or `cause`, a `reason` or an argument), is a
    timeout of any HTTP client: a type named `Timeout`, `ReadTimeout`,
    `ConnectTimeoutError`, ..., or `TimeoutError` itself. The client libraries
    are matched by name so none has to be imported. urllib3's
    `NewConnectionError` derives from its `ConnectTimeoutError` but means the
    connection was refused or the name did not resolve: it is not one."""
    pending = [error]
    for _ in range(8):
        if not pending:
            break
        current = pending.pop()
        if current is None:
            continue
        names = {cls.__name__ for cls in type(current).__mro__}
        if "NewConnectionError" in names:
            continue
        if any(
            name.endswith(("Timeout", "TimeoutError")) or name == "timeout"
            for name in names
        ):
            return True
        for attr in ("__cause__", "__context__", "inner_exception", "cause", "reason"):
            pending.append(getattr(current, attr, None))
        pending.extend(
            arg
            for arg in getattr(current, "args", ())
            if isinstance(arg, BaseException)
        )
    return False


#: Backend attributes that are the live client and what guards it: left out
#: of a pickled or copied backend, which builds its own on first use.
_LIVE = frozenset({"_client", "_client_lock", "__dict__", "__weakref__"})


def lazy_client(backend, build):
    """`backend._client`, made by `build()` the first time under
    `backend._client_lock` so that threads racing for it build one."""
    client = backend._client
    if client is None:
        with backend._client_lock:
            client = backend._client
            if client is None:
                client = backend._client = build()
    return client


def backend_state(backend) -> dict:
    """What a pickled or copied backend keeps: its slots and `__dict__`,
    without the client it built and the lock that guarded the build."""
    state = {}
    for cls in type(backend).__mro__:
        slots = getattr(cls, "__slots__", ())
        for name in (slots,) if isinstance(slots, str) else slots:
            if name not in _LIVE and hasattr(backend, name):
                state[name] = getattr(backend, name)
    state.update(
        (name, value)
        for name, value in getattr(backend, "__dict__", {}).items()
        if name not in _LIVE
    )
    return state


def restore_backend(backend, state: dict) -> None:
    """The inverse of `backend_state()`: a backend with no client yet."""
    for name, value in state.items():
        setattr(backend, name, value)
    backend._client = None
    backend._client_lock = _threading.Lock()


def unreachable_keys(
    prefix: str, entries: "_ty.Iterable[tuple[str, int]]"
) -> "list[str]":
    """The keys among `entries` (`(key, size)` pairs, a flat listing of
    `prefix`) that a walk of the directory `prefix` never visits.

    `prefix` is `"dir/"`, or `""` for a bucket or container root. A key is
    reached when every name below the prefix is one path component, no
    directory on the way is also an object (the object wins and its subtree is
    not listed), and a key ending in `/` is an empty marker rather than data.
    The prefix's own marker (`prefix` itself) is reached.
    """
    entries = list(entries)
    keys = {key for key, _size in entries}
    unreachable = []
    for key, size in entries:
        rest = key[len(prefix) :]
        if not rest:
            continue
        *directories, leaf = rest.split("/")
        marker = leaf == ""
        if not all(_utils.is_safe_child_name(name) for name in directories) or (
            not marker and not _utils.is_safe_child_name(leaf)
        ):
            unreachable.append(key)
        elif marker and size:
            unreachable.append(key)
        elif any(
            prefix + "/".join(directories[:depth]) in keys
            for depth in range(1, len(directories) + 1)
        ):
            unreachable.append(key)
    return unreachable


def refusal(path, action: str, keys: "list[str]") -> OSError:
    """The error for `action` ("copy" or "remove") on a prefix that holds
    `keys` a directory walk cannot reach; nothing has been changed."""
    shown = ", ".join(repr(key) for key in keys[:5])
    more = f" and {len(keys) - 5} more" if len(keys) > 5 else ""
    return OSError(
        _errno.EINVAL,
        f"cannot {action} a prefix holding keys a listing does not show "
        f"({shown}{more}); nothing was changed",
        str(path),
    )


@_contextlib.contextmanager
def checked_copy(path, recursive: bool):
    """Around `Path.copy()` of an object-store `path`: a recursive copy of a
    prefix is refused, before anything is created, when it holds keys the
    walk would leave behind. Children are copied through the same method;
    only the outermost call checks."""
    if not recursive or getattr(_state, "copying", False):
        yield
        return
    unreachable = path._unreachable_keys()
    if unreachable:
        raise refusal(path, "copy", unreachable)
    _state.copying = True
    try:
        yield
    finally:
        _state.copying = False


class BufferedUploadStream(_io.BytesIO):
    """The write stream of a store that uploads a whole object at once: it
    buffers the writes and calls `path._upload(data, exclusive=)` on close().
    `exclusive` (`open("x")`) makes the upload a conditional create. With
    `initial` (`open("r+")`) the buffer starts with the object's content at
    position 0 and is uploaded only if it was modified."""

    def __init__(self, path, exclusive=False, initial=None):
        super().__init__(b"" if initial is None else initial)
        self._path = path
        self._exclusive = exclusive
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
                self._path._upload(self.getvalue(), exclusive=self._exclusive)
        finally:
            # Closed even when the upload fails, so `IOBase.__del__` does not
            # retry it (over newer content) at garbage collection.
            super().close()


class ObjectStorePath(_UriPath):
    """The algorithms `s3:`, `gs:` and `az:` share: a key namespace with no
    directories, where a directory is any prefix `key/` that has keys under it
    and `mkdir()` writes an empty `key/` marker. A store supplies the calls
    that differ -- one SDK request each, errors already translated to
    `OSError` (see `transport_error()`) -- and gets `stat()`, `rm()`,
    `unlink()`, `rmdir()`, `rename()`, `copy()` and the rest from here.

    Primitives a subclass defines:

    - `_stat_root()` -> `FileStat`: the bucket, container or account exists.
    - `_stat_object(key)` -> `FileStat | None`: None when no object has that
      exact key.
    - `_has_object(key)` -> bool, `_has_prefix(key)` -> bool: whether an
      object has that key, whether any key lies under `"<key>/"` (a one-item
      listing).
    - `_flat_entries(prefix)` -> `[(key, size)]`: every key under `prefix`.
    - `_first_keys(prefix, limit)` -> `[key]`: at most `limit` keys under it.
    - `_put_marker(key)`, `_delete_object(key, missing_ok=)`,
      `_delete_keys(keys, on_error)`.
    - `_rename_source(key)` -> a store-specific record, or None when there is
      no object; `_rename_copy(source, dest, dest_key)`.
    """

    __slots__ = ()

    #: What the store calls its top level, for the refusals to remove it.
    _TOP = "bucket"
    #: Exceptions `rm(recursive=True)` offers to `ignore_error` when listing a
    #: prefix fails (a missing bucket is not one).
    _LISTING_ERRORS: "tuple[type[BaseException], ...]" = (OSError,)

    def _stat_root(self) -> FileStat:
        raise NotImplementedError

    def _stat_object(self, key: str) -> "FileStat | None":
        raise NotImplementedError

    def _has_object(self, key: str) -> bool:
        raise NotImplementedError

    def _has_prefix(self, key: str) -> bool:
        raise NotImplementedError

    def _flat_entries(self, prefix: str) -> "list[tuple[str, int]]":
        raise NotImplementedError

    def _first_keys(self, prefix: str, limit: int) -> "list[str]":
        raise NotImplementedError

    def _put_marker(self, key: str) -> None:
        raise NotImplementedError

    def _delete_object(self, key: str, *, missing_ok: bool) -> None:
        raise NotImplementedError

    def _delete_keys(self, keys: "list[str]", on_error) -> None:
        raise NotImplementedError

    def _rename_source(self, key: str):
        raise NotImplementedError

    def _rename_copy(self, source, dest: "ObjectStorePath", dest_key: str) -> None:
        raise NotImplementedError

    def _rename_dest(self, target) -> "ObjectStorePath":
        """The path `rename(target)` produces."""
        return self.with_path(target.path)

    def _key_path(self, key: str) -> "ObjectStorePath":
        """The path of the object `key` in this path's bucket."""
        return self.with_path(f"/{key}")

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        key = self.key
        if key == "":
            return self._stat_root()
        found = self._stat_object(key)
        if found is not None:
            return found
        return self._stat_prefix()

    def _stat_prefix(self) -> FileStat:
        """`stat()` of a key already known not to be an object -- emulate a
        directory: any object under the "<key>/" prefix means this is a
        "directory", and none means there is nothing here."""
        if self._has_prefix(self.key):
            return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _holds_keys(self, key: str) -> bool:
        """Whether any key lies under the prefix `key/`: a write or rename
        onto it would hide those keys behind an object. Credentials that may
        write but not list cannot ask, and are not stopped."""
        if not key:
            return False
        try:
            return self._has_prefix(key)
        except PermissionError:
            return False

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def _mkdir(self, mode):
        # stat(), not exists(): a failed probe must not read as "missing".
        try:
            self.stat()
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(self)
        self._put_marker(f"{self.key}/")

    def unlink(self, missing_ok=False):
        try:
            st = self.stat()
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if st.is_dir():
            # A prefix: deleting the bare key would delete nothing (S3) or
            # something else.
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        self._delete_object(self.key, missing_ok=missing_ok)

    def rmdir(self):
        if not self.stat().is_dir():
            raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
        if not self.key:
            raise PermissionError(
                _errno.EACCES, f"removing a {self._TOP} is not supported", str(self)
            )
        marker = f"{self.key}/"
        if any(name != marker for name in self._first_keys(marker, 2)):
            raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
        self._delete_object(marker, missing_ok=True)

    def rm(
        self,
        /,
        recursive=False,
        missing_ok=False,
        ignore_error: "bool | _ty.Callable[[Exception, _ty.Self], bool]" = False,
        *,
        follow_symlinks=False,
        follow_binds=False,
    ):
        # An object store holds no symlinks or bindings: the policies are
        # checked and have nothing to decide.
        _check_follow("follow_symlinks", follow_symlinks)
        _check_follow("follow_binds", follow_binds)
        if not recursive:
            return super().rm(
                recursive=recursive,
                missing_ok=missing_ok,
                ignore_error=ignore_error,
                follow_symlinks=follow_symlinks,
                follow_binds=follow_binds,
            )

        def on_error(error, path=None):
            if callable(ignore_error):
                return ignore_error(error, self if path is None else path)
            return bool(ignore_error)

        if not self.key:
            error = PermissionError(f"recursive {self._TOP} delete is not enabled")
            if not on_error(error):
                raise error
            return

        keys = []
        try:
            if self._has_object(self.key):
                keys.append(self.key)
        except OSError as error:
            # Not "missing": deleting the prefix tree instead of the object
            # would remove the wrong thing.
            if not on_error(error):
                raise
            return

        if not keys:
            marker = f"{self.key}/"
            try:
                entries = self._flat_entries(marker)
            except FileNotFoundError:
                # A missing bucket holds nothing: the same answer as a missing key.
                entries = []
            except self._LISTING_ERRORS as error:
                if not on_error(error):
                    raise
                return
            keys = [key for key, _size in entries]
            # What a walk of the directory would not visit is not removed
            # with it: a move copies by walking and then removes this set.
            unreachable = unreachable_keys(marker, entries)
            if unreachable:
                error = refusal(self, "remove", unreachable)
                if not on_error(error):
                    raise error
                skipped = set(unreachable)
                keys = [key for key in keys if key not in skipped]
                if not keys:
                    return

        if not keys:
            if missing_ok:
                return
            error = FileNotFoundError(self)
            if not on_error(error):
                raise error
            return

        self._delete_keys(keys, on_error)

    def _unreachable_keys(self) -> "list[str]":
        """The keys of this prefix directory that a walk does not reach, or
        `[]` for an object or a missing path."""
        key = self.key
        if key and self._has_object(key):
            return []
        prefix = f"{key}/" if key else ""
        return unreachable_keys(prefix, self._flat_entries(prefix))

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
        # A prefix is copied by walking it; one that holds keys the walk
        # cannot reach is refused up front rather than copied incompletely.
        with checked_copy(self, recursive):
            return super().copy(
                target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                recursive=recursive,
                ignore_error=ignore_error,
                progress=progress,
            )

    def rename(self, target):
        target = self._rename_target(target)
        dest = self._rename_dest(target)
        dest_key = dest.key
        if dest_key == self.key:
            # Copying onto itself and then deleting the source loses the
            # object; a name that is not there is not renamed. pathlib
            # returns the new path.
            if not self._has_object(self.key):
                self._stat_prefix()
            return dest
        source = self._rename_source(self.key)
        if source is None:
            # No object at the key: a prefix directory (FileNotFoundError
            # when there is nothing at all). Its keys are not renamed one by
            # one here; move() falls back to copy + rm.
            self._stat_prefix()
            raise NotImplementedError(f"rename() of the prefix directory {self}")
        if self._holds_keys(dest_key):
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(target))
        self._rename_copy(source, dest, dest_key)
        return dest
