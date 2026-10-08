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
import threading as _threading
import typing as _ty

from ... import utils as _utils

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
