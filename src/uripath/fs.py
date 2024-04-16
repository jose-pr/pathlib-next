import stat as _stat

from . import utils as _utils


class FileStat:
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