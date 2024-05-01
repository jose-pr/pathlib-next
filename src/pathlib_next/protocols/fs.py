import abc as _abc
import stat as _stat
import typing as _ty

from .. import utils as _utils


class FileStatLike(_ty.Protocol):
    """Minimum properties stat like object shouuld provide"""

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
            return self.stat().st_mode
        except FileNotFoundError:
            return None
        except ValueError:
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
    __slots__ = ()

    @_utils.notimplemented
    def chmod(self, mode: int, *, follow_symlinks=True):
        """
        Change the permissions of the path, like os.chmod().
        """
        ...

    def lchmod(self, mode: int):
        """
        Like chmod(), except if the path points to a symlink, the symlink's
        permissions are changed, rather than its target's.
        """
        self.chmod(mode, follow_symlinks=False)
