import abc as _abc
import stat as _stat
import typing as _ty

from .. import utils as _utils
from ..protocols import FileStatLike as _FStat

if _ty.TYPE_CHECKING:
    from os import stat_result as _os_stat

    from ..path import Path


class FileStat(_FStat):
    __slots__ = (
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_atime",
        "st_mtime",
        "st_ctime",
    )

    def __init__(
        self,
        st_mode: int = None,
        st_size: int = 0,
        st_mtime: int = 0,
        is_dir: bool = False,
    ):
        self.st_mode = st_mode or (
            _stat.S_IFDIR | 0o555 if is_dir else _stat.S_IFREG | 0o444
        )
        self.st_nlink = 1
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = st_size
        self.st_atime = 0
        self.st_mtime = st_mtime
        self.st_ctime = 0

    def settime(self, value):
        self.st_atime = self.st_mtime = self.st_ctime = value

    def setmode(self, value, isdir=None):
        if isdir is None:
            isdir = self.st_mode & _stat.S_IFDIR
        if isdir:
            self.st_mode = _stat.S_IFDIR | value
        else:
            self.st_mode = _stat.S_IFREG | value

    def __getitem__(self, key):
        return getattr(self, key)

    def items(self):
        for key in self.__slots__:
            yield key, getattr(self, key)

    def __repr__(self):
        return "<%s mode=%o, size=%s, mtime=%d>" % (
            type(self).__name__,
            self.st_mode,
            _utils.sizeof_fmt(self.st_size),
            self.st_mtime,
        )

    def __str__(self):
        props = [f"{k}={v}" for k, v in self.items()]
        return "<%s %s>" % (type(self).__name__, ", ".join(props))

    @classmethod
    def from_path(cls, path: "Path", *, follow_symlink=True):
        try:
            stat: _os_stat = path.stat(follow_symlinks=follow_symlink)
            if isinstance(stat, FileStat):
                return stat
            else:
                result = FileStat.__new__(FileStat)
                for prop in FileStat.__slots__:
                    val = getattr(stat, prop, 0)
                    setattr(result, prop, val)
                return result
        except FileNotFoundError:
            return None

    def is_dir(self):
        """
        Whether this path is a directory.
        """
        return _stat.S_ISDIR(self.st_mode)

    def is_file(self):
        """
        Whether this path is a regular file (also True for symlinks pointing
        to regular files).
        """
        return _stat.S_ISREG(self.st_mode)

    def is_symlink(self):
        """
        Whether this path is a symbolic link.
        """
        return _stat.S_ISLNK(self.st_mode)

    def is_block_device(self):
        """
        Whether this path is a block device.
        """
        return _stat.S_ISBLK(self.st_mode)

    def is_char_device(self):
        """
        Whether this path is a character device.
        """
        return _stat.S_ISCHR(self.st_mode)

    def is_fifo(self):
        """
        Whether this path is a FIFO.
        """
        return _stat.S_ISFIFO(self.st_mode)

    def is_socket(self):
        """
        Whether this path is a socket.
        """
        return _stat.S_ISSOCK(self.st_mode)
