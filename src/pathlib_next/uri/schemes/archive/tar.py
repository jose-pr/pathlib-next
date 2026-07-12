from __future__ import annotations

import io as _io
import tarfile as _tarfile

from ....utils.stat import FileStat
from ._base import ArchiveUri, _ArchiveBackend


class _TarBackend(_ArchiveBackend):
    __slots__ = ()

    def _open(self):
        return _tarfile.open(fileobj=_io.BytesIO(self.outer.read_bytes()), mode="r:*")

    def names(self):
        return self.handle.getnames()

    def read_member(self, path):
        try:
            member = self.handle.getmember(path)
        except KeyError:
            raise
        f = self.handle.extractfile(member)
        if f is None:
            raise IsADirectoryError(path)
        return f

    def member_stat(self, path):
        info = self.handle.getmember(path)
        return FileStat(st_size=info.size, st_mtime=int(info.mtime), is_dir=info.isdir())


class TarUri(ArchiveUri):
    """`tar:` scheme (also handles `.tar.gz`/`.tar.bz2`/`.tar.xz` via
    `tarfile`'s auto-detected "r:*" mode). Read-only."""

    __SCHEMES = ("tar",)
    __slots__ = ()
    _backend_cls = _TarBackend
