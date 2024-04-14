from io import IOBase
from typing import Iterable

from uripath.io import FileStat
from .uri import UriPath


class LocalPath(UriPath):
    _SCHEMES_ = ["file"]
    __slots__ = ( "_local_", )

    @property
    def _local(self):
        if self._local_ is None:
            self._local_ = self.as_localpath()
        return self._local_
    
    def iterdir(self):
        for path in self._local.iterdir():
            return LocalPath(path)

    def stat(self, *, follow_symlinks=True):
        return self._local.stat(follow_symlinks=follow_symlinks)

    def open(
        self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
    ) -> IOBase:
        return self._local.open(mode, buffering, encoding, errors, newline)

    def mkdir(self, mode=511, parents=False, exist_ok=False):
        return self._local.mkdir(mode, parents, exist_ok)

    def chmod(self, mode, *, follow_symlinks=True):
        return self._local.chmod(mode, follow_symlinks=follow_symlinks)

    def unlink(self, missing_ok=False):
        return self._local.unlink(missing_ok)

    def rmdir(self):
        return self._local.rmdir()
    
    def _rename(self, target):
        return self._local.rename(target)
    
