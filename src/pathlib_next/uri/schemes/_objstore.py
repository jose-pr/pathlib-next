"""What `s3:`, `gs:` and `az:` share: which keys of a prefix a directory walk
can reach, and the guard that keeps a recursive copy and a recursive remove
to the same set of keys.

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
