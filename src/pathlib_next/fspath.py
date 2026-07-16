from __future__ import annotations

import functools as _func
import ntpath as _ntpath
import os as _os
import pathlib as _path
import posixpath as _posixpath
import re as _re
import sys as _sys
import types as _types
import typing as _ty

from . import path as _proto
from .utils.stat import FileStat as _FileStat

# pathlib.Path.stat()/chmod() only accept follow_symlinks= on 3.10+; below
# that, LocalPath (which inherits them directly from pathlib.Path via MRO,
# see class LocalPath below) needs a shim.
_HAS_FOLLOW_SYMLINKS = _sys.version_info >= (3, 10)


@_func.cache
def _is_case_sensitive(flavour: _os.path) -> bool:
    return flavour.normcase("Aa") == "Aa"


class _BaseFSPathname(_path.PurePath, _proto.Pathname):
    __slots__ = ()

    @property
    def _parser(self) -> _os.path:
        try:
            # 3.13+: renamed to `parser`, already a module.
            return self.parser
        except AttributeError:
            pass
        flavour = self._flavour
        if isinstance(flavour, _types.ModuleType):
            # 3.12: `_flavour` is already a module (ntpath/posixpath).
            return flavour
        # 3.9-3.11: `_flavour` is a `_WindowsFlavour`/`_PosixFlavour` object
        # with no `normcase`/`pathsep`/etc — bridge to the equivalent module.
        return _ntpath if flavour.sep == "\\" else _posixpath

    @property
    def _path_separators(self) -> _ty.Sequence[str]:
        parser = self._parser
        return (parser.sep,) + ((parser.altsep,) if parser.altsep else ())

    @property
    def _is_case_sensitive(self) -> bool:
        return _is_case_sensitive(self._parser)

    @property
    def segments(self):
        return self.parts

    def with_segments(self, *args: str | _proto.FsPathLike):
        return type(self)(*args)


class PosixPathname(_path.PurePosixPath, _BaseFSPathname):
    """Pure (no I/O) POSIX-flavour path, implementing the `Pathname`
    protocol on top of `pathlib.PurePosixPath`."""

    __slots__ = ()


class WindowsPathname(_path.PureWindowsPath, _BaseFSPathname):
    """Pure (no I/O) Windows-flavour path, implementing the `Pathname`
    protocol on top of `pathlib.PureWindowsPath`."""

    __slots__ = ()


class LocalPath(
    _path.WindowsPath if _os.name == "nt" else _path.PosixPath,
    _proto.Path,
    _BaseFSPathname,
):
    """The real local filesystem path: `pathlib.WindowsPath`/`PosixPath`
    with this library's `Path` mixed in via MRO. Behaves exactly like
    `pathlib.Path` for anything not explicitly overridden here (see
    `docs/divergences.md`)."""

    __slots__ = ()

    def _scandir(self):
        # On 3.11+, `pathlib.Path._scandir()` (stdlib, ahead of ours in the
        # MRO via WindowsPath/PosixPath) shadows `_proto.Path._scandir()`
        # and returns `os.scandir(self)` directly -- an iterator of raw
        # `os.DirEntry`, not this project's `(name, FileStat|None)` tuples.
        # walk()/glob() expect the latter, so re-assert our own contract
        # here regardless of what stdlib does in a given version. DirEntry's
        # own cached lstat (`follow_symlinks=False`, matching walk()'s
        # default) is reused instead of a fresh stat() round trip.
        for entry in _os.scandir(self):
            try:
                stat = _FileStat.from_stat(entry.stat(follow_symlinks=False))
            except OSError:
                stat = None
            yield entry.name, stat

    def stat(self, *, follow_symlinks=True):
        # pathlib.Path.stat() (next in MRO via WindowsPath/PosixPath) only
        # accepts follow_symlinks= on 3.10+; below that, lstat() is the
        # (pre-existing, non-kwarg) equivalent for follow_symlinks=False.
        if _HAS_FOLLOW_SYMLINKS:
            return super().stat(follow_symlinks=follow_symlinks)
        return super().stat() if follow_symlinks else super().lstat()

    def chmod(self, mode, *, follow_symlinks=True):
        # Same follow_symlinks= 3.10+ gap as stat() above; lchmod() is the
        # pre-existing equivalent (raises NotImplementedError itself on
        # platforms without os.lchmod, e.g. Windows).
        if _HAS_FOLLOW_SYMLINKS:
            return super().chmod(mode, follow_symlinks=follow_symlinks)
        return (
            super().chmod(mode) if follow_symlinks else super().lchmod(mode)
        )

    def glob(
        self,
        pattern: str | _proto.FsPathLike,
        *,
        case_sensitive: bool = None,
        include_hidden: bool = False,
        recursive: bool = None,
        dironly: bool = None,
    ):
        """Iterate over this subtree and yield all existing files (of any
        kind, including directories) matching the given relative pattern.

        A "**" pattern component auto-enables recursion (pathlib parity);
        see Path.glob()'s docstring for the `recursive=` override rules.
        """
        if not isinstance(pattern, (str, _re.Pattern)):
            pattern = _os.fspath(pattern)
        if dironly is None:
            dironly = (
                isinstance(pattern, str)
                and pattern
                and pattern[-1] in self._path_separators
            )
        yield from _proto.Path.glob(
            self,
            pattern,
            case_sensitive=case_sensitive,
            include_hidden=include_hidden,
            recursive=recursive,
            dironly=dironly,
        )
