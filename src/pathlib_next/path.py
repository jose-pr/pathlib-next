import pathlib as _path
import os as _os
import functools as _func
import typing as _ty
from . import protocols as _proto
import re as _re


class PurePath(_path.PurePath, _proto.PurePathProtocol):
    __slots__ = ()

    _flavour: _os.path

    @property
    def _path_separators(self) -> _ty.Sequence[str]:
        return (self._flavour.pathsep, self._flavour.altsep)

    @property
    @_func.cache
    def _is_case_sensitive(self) -> bool:
        return self._flavour.normcase("Aa") == "Aa"

    def _make_child_relpath(self, name: str) -> _ty.Self:
        return _path.Path._make_child_relpath(self, name)


class PurePosixPath(_path.PurePosixPath, PurePath):
    __slots__ = ()


class PureWindowsPath(_path.PureWindowsPath, PurePath):
    __slots__ = ()


class Path(_path.Path, _proto.PathProtocol, PurePath):
    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        if cls is Path:
            cls = WindowsPath if _os.name == "nt" else PosixPath
        return object.__new__(cls)

    def glob(
        self,
        pattern: str | _proto.FsPath,
        *,
        case_sensitive: bool = None,
        include_hidden: bool = False,
        recursive: bool = False,
        dironly: bool = False,
    ):
        """Iterate over this subtree and yield all existing files (of any
        kind, including directories) matching the given relative pattern.
        """
        if not isinstance(pattern, (str, _re.Pattern)):
            pattern = _os.fspath(pattern)
        if dironly is None:
            dironly = (
                isinstance(pattern, str)
                and pattern
                and pattern[-1] in self._path_separators
            )
        yield from _proto.PathProtocol.glob(
            self,
            self,
            pattern,
            case_sensitive=case_sensitive,
            include_hidden=include_hidden,
            recursive=recursive,
            dironly=dironly,
        )

    def rglob(
        self,
        pattern: str,
        *,
        case_sensitive: bool = None,
        include_hidden: bool = False,
        dironly: bool = False,
    ):
        """Recursively yield all existing files (of any kind, including
        directories) matching the given relative pattern, anywhere in
        this subtree.
        """
        yield from self.glob(
            pattern,
            case_sensitive=case_sensitive,
            include_hidden=include_hidden,
            recursive=True,
            dironly=dironly,
        )


class PosixPath(Path, PurePosixPath):
    """Path subclass for non-Windows systems.

    On a POSIX system, instantiating a Path should return this object.
    """

    __slots__ = ()

    if _os.name == "nt":

        def __new__(cls, *args, **kwargs):
            raise NotImplementedError(
                f"cannot instantiate {cls.__name__!r} on your system"
            )


class WindowsPath(Path, PureWindowsPath):
    """Path subclass for Windows systems.

    On a Windows system, instantiating a Path should return this object.
    """

    __slots__ = ()

    if _os.name != "nt":

        def __new__(cls, *args, **kwargs):
            raise NotImplementedError(
                f"cannot instantiate {cls.__name__!r} on your system"
            )
