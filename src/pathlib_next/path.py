import pathlib as _path
import os as _os
import functools as _func
import typing as _ty
from . import protocols as _proto


class PurePath(_path.PurePath, _proto.PurePathProtocol):
    __slots__ = ()

    _flavour: _os.path

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


class Path(_path.Path, _proto.PathProtocol):
    __slots__ = ()

    def glob(
        self,
        pattern: str,
        *,
        case_sensitive: bool = None,
        include_hidden: bool = False,
        recursive: bool = False,
    ):
        """Iterate over this subtree and yield all existing files (of any
        kind, including directories) matching the given relative pattern.
        """
        yield from _proto.PathProtocol.glob(
            self,
            self,
            pattern,
            case_sensitive=case_sensitive,
            include_hidden=include_hidden,
            recursive=recursive,
        )

    def rglob(
        self, pattern: str, *, case_sensitive: bool = None, include_hidden: bool = False
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
        )
