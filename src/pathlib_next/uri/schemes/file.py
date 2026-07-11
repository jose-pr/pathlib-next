from __future__ import annotations

import os as _os
import sys as _sys

from ...fspath import LocalPath as _Local
from ...path import FsPathLike
from .. import Source, UriPath

# pathlib.Path.stat()/chmod() only accept follow_symlinks= on 3.10+.
_HAS_FOLLOW_SYMLINKS = _sys.version_info >= (3, 10)


class FileUri(UriPath):
    __SCHEMES = ("file",)
    __slots__ = ("_filepath",)

    @property
    def filepath(self):
        if self._filepath is None:
            self._filepath = _Local(self.__fspath__())
        return self._filepath

    def _init(
        self,
        source: Source,
        path: str,
        query: str,
        fragment: str,
        /,
        **kwargs,
    ):
        if _os.name == "nt" and path and path[0] == "/":
            root, *_ = path[1:].split("/", maxsplit=1)
            if root and root[-1] == ":":
                path = path.removeprefix("/")
        super()._init(source, path, query, fragment, **kwargs)

    def _listdir(self):
        yield from _os.listdir(self.filepath)

    def stat(self, *, follow_symlinks=True):
        if _HAS_FOLLOW_SYMLINKS:
            return self.filepath.stat(follow_symlinks=follow_symlinks)
        if not follow_symlinks:
            raise NotImplementedError(
                "stat(follow_symlinks=False) requires Python 3.10+"
            )
        return self.filepath.stat()

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        return self.filepath.open(mode, buffering, encoding, errors, newline)

    def mkdir(self, mode=511, parents=False, exist_ok=False):
        return self.filepath.mkdir(mode, parents, exist_ok)

    def chmod(self, mode, *, follow_symlinks=True):
        if _HAS_FOLLOW_SYMLINKS:
            return self.filepath.chmod(mode, follow_symlinks=follow_symlinks)
        if not follow_symlinks:
            raise NotImplementedError(
                "chmod(follow_symlinks=False) requires Python 3.10+"
            )
        return self.filepath.chmod(mode)

    def unlink(self, missing_ok=False):
        return self.filepath.unlink(missing_ok)

    def rmdir(self):
        return self.filepath.rmdir()

    def rename(self, target: FsPathLike | str):
        try:
            _target = _os.fspath(target)
        except (TypeError, NotImplementedError):
            _target = NotImplemented

        if _target is NotImplemented:
            raise NotImplementedError("rename", target)

        return self.filepath.rename(_target)
