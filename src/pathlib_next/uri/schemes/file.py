from __future__ import annotations

import os as _os

from ...fspath import LocalPath as _Local
from ...path import FsPathLike
from .. import Source, UriPath


class FileUri(UriPath):
    """`file:` scheme: a `UriPath` wrapping a `LocalPath` (`filepath`),
    delegating all I/O to it."""

    __SCHEMES = ("file",)
    __slots__ = ("_filepath",)

    @property
    def filepath(self):
        if self._filepath is None:
            self._filepath = _Local(self.__fspath__())
        return self._filepath

    @property
    def parent(self):
        parent = super().parent
        path = parent.path
        if _os.name == "nt" and len(path) == 2 and path[1] == ":" and path[0].isalpha():
            # "C:" alone is drive-RELATIVE (the current directory on C:); the
            # parent of a top-level "C:/Windows" is the drive root "C:/".
            return parent.with_path(path + "/")
        return parent

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

    def _scandir(self):
        # LocalPath's scandir already carries each entry's lstat (one syscall
        # per directory, not one per child).
        yield from self.filepath._scandir()

    def _is_junction_link(self) -> bool:
        return self.filepath._is_junction_link()

    def stat(self, *, follow_symlinks=True):
        # LocalPath.stat() itself shims the 3.10+-only follow_symlinks= kwarg
        # for pathlib.Path (see fspath.py), so just delegate.
        return self.filepath.stat(follow_symlinks=follow_symlinks)

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        return self.filepath.open(mode, buffering, encoding, errors, newline)

    def mkdir(self, mode=511, parents=False, exist_ok=False):
        return self.filepath.mkdir(mode, parents, exist_ok)

    def touch(self, mode=None, exist_ok=True):
        # pathlib's touch: the mode goes through os.open(), so the umask
        # applies, and an existing file's mtime is bumped. The generic
        # Path.touch can do neither.
        if mode is None:
            return self.filepath.touch(exist_ok=exist_ok)
        return self.filepath.touch(mode, exist_ok)

    def chmod(self, mode: int | str, *, follow_symlinks: bool = True):
        # LocalPath.chmod() itself shims the 3.10+-only follow_symlinks=
        # kwarg for pathlib.Path (see fspath.py) and normalizes a string
        # octal mode, so just delegate.
        return self.filepath.chmod(mode, follow_symlinks=follow_symlinks)

    def _chown(
        self,
        uid: int | str | None,
        gid: int | str | None,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        # Delegate to LocalPath's primitive; chown() has already
        # canonicalized the pair, so pass it through untouched.
        return self.filepath._chown(uid, gid, follow_symlinks=follow_symlinks)

    def unlink(self, missing_ok=False):
        return self.filepath.unlink(missing_ok)

    def rmdir(self):
        return self.filepath.rmdir()

    def rename(self, target: FsPathLike | str):
        # Through `_rename_target()` like every other scheme: a relative str
        # is a sibling rename (it used to resolve against the process cwd),
        # a local path is accepted, and a remote Uri raises
        # NotImplementedError so move() copies instead of renaming locally.
        dest = self._rename_target(target)
        if not isinstance(dest, FileUri):
            dest = self._from_parsed_parts(
                dest.source or self.source, dest.path, None, None
            )
        self.filepath.rename(dest.filepath)
        return dest
