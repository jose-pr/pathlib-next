from __future__ import annotations

import abc as _abc
import stat as _stat
import typing as _ty

from .. import utils as _utils


class FileStatLike(_ty.Protocol):
    """Minimum properties stat like object should provide"""

    __slots__ = ()

    @property
    @_abc.abstractmethod
    def st_mode(self) -> int: ...
    @property
    @_abc.abstractmethod
    def st_size(self) -> int: ...
    @property
    @_abc.abstractmethod
    def st_mtime(self) -> int: ...


class Stat(_ty.Protocol):
    """Any object that can implement Stat and utilities functions based on it"""

    __slots__ = ()

    @_utils.notimplemented
    def stat(self, *, follow_symlinks=True) -> FileStatLike: ...

    def lstat(self) -> FileStatLike:
        """
        Like stat(), except if the path points to a symlink, the symlink's
        status information is returned, rather than its target's.
        """
        return self.stat(follow_symlinks=False)

    def _st_mode(self, *, follow_symlinks=True):
        """
        Utility function only for internal use if this object,
        not required nor to be expected in any implementations of the protocol
        """
        try:
            # follow_symlinks must be forwarded -- it wasn't, so is_symlink()
            # (which calls this with follow_symlinks=False) always resolved
            # through the symlink instead of stat'ing it directly.
            return self.stat(follow_symlinks=follow_symlinks).st_mode
        except (OSError, ValueError):
            # pathlib.Path.exists()/is_dir()/etc. swallow OSError (including
            # PermissionError) and report False rather than propagating it.
            return None

    # Convenience functions for querying the stat results
    def exists(self, *, follow_symlinks=True):
        """
        Whether this path exists.
        """
        return self._st_mode(follow_symlinks=follow_symlinks) != None

    def is_dir(self):
        """
        Whether this path is a directory.
        """
        return _stat.S_ISDIR(self._st_mode() or 0)

    def is_file(self):
        """
        Whether this path is a regular file (also True for symlinks pointing
        to regular files).
        """
        return _stat.S_ISREG(self._st_mode() or 0)

    def is_symlink(self):
        """
        Whether this path is a symbolic link.
        """
        return _stat.S_ISLNK(self._st_mode(follow_symlinks=False) or 0)

    def is_block_device(self):
        """
        Whether this path is a block device.
        """
        return _stat.S_ISBLK(self._st_mode() or 0)

    def is_char_device(self):
        """
        Whether this path is a character device.
        """
        return _stat.S_ISCHR(self._st_mode() or 0)

    def is_fifo(self):
        """
        Whether this path is a FIFO.
        """
        return _stat.S_ISFIFO(self._st_mode() or 0)

    def is_socket(self):
        """
        Whether this path is a socket.
        """
        return _stat.S_ISSOCK(self._st_mode() or 0)


class Chmod(_ty.Protocol):
    """Composable protocol for objects supporting permission changes."""

    __slots__ = ()

    @_utils.notimplemented
    def chmod(self, mode: int | str, *, follow_symlinks: bool = True):
        """
        Change the permissions of the path, like os.chmod().

        Extension: `mode` may be a `str`, which is parsed as **octal**
        (`"0755"`, `"755"` and `0o755` all mean the same thing) -- see
        `utils.as_mode()` for why the base is never left implicit.

        Every implementation normalizes its argument through
        `utils.as_mode()` on entry. Unlike `symlink_to`/`chown`, `chmod` is
        overridden directly by each backend (each has real per-scheme logic
        -- version shims, capability gates, a `SITE CHMOD` command), so
        there is no single wrapper to hang the conversion on; the shared
        helper is what keeps the base from drifting between them.
        """
        ...

    def lchmod(self, mode: int | str):
        """
        Like chmod(), except if the path points to a symlink, the symlink's
        permissions are changed, rather than its target's.
        """
        self.chmod(mode, follow_symlinks=False)

    @_utils.notimplemented
    def _chown(
        self,
        uid: int | str | None,
        gid: int | str | None,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        """Backend primitive behind `chown()`.

        Receives an already-canonical `(uid, gid)` pair: `int` to set, or
        `None` for "leave unchanged" (a `str` is a user/group *name*).
        Convert only to this backend's own wire spelling -- `-1` for
        `os.chown`, an omitted attr field for SFTP `setstat`. Normalizing
        the caller's input is `chown()`'s job, not the primitive's.
        """
        ...

    def chown(
        self,
        uid: int | str | None = None,
        gid: int | str | None = None,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        """Change the owner and/or group of the path.

        Extension: `pathlib.Path` has `owner()`/`group()` **readers** but no
        writer (the stdlib equivalent is `shutil.chown`/`os.chown`), so
        ownership was the one attribute `stat()` could report -- via
        `st_uid`/`st_gid` -- and nothing could write back.

        `None` (the default) leaves a field unchanged, which is what lets a
        caller set the group without knowing the owner. `-1` is accepted as
        an alias for `None`, matching `os.chown`'s own sentinel. An `int` is
        a uid/gid; a `str` is a user/group name, passed through for backends
        that can resolve one.

        Backends implement `_chown()` and receive an already-canonical pair
        (see `utils.as_owner()`), so the "unchanged" semantics cannot drift
        between schemes.
        """
        uid, gid = _utils.as_owner(uid, gid)
        if uid is None and gid is None:
            # Nothing to change -- don't spend a round trip (or make a
            # backend decide what an all-sentinel call means).
            return
        return self._chown(uid, gid, follow_symlinks=follow_symlinks)
