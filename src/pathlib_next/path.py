"""Object-oriented filesystem paths.

This module provides classes to represent abstract paths and concrete
paths with operations that have semantics appropriate for different
operating systems.
"""

from __future__ import annotations

import abc as _abc
import os as _os
import re as _re
import typing as _ty

from . import utils as _utils
from .protocols import BinaryOpen, Chmod, Stat
from .utils import glob as _glob
from .utils.stat import FileStat

P = _ty.TypeVar("P", bound="Path")
PN = _ty.TypeVar("PN", bound="Pathname")

_P = _ty.TypeVar("_P")


class FsPathLike(_ty.Protocol):
    """Anything implementing `__fspath__` -- registered with `os.PathLike`
    so `os.fspath()` and friends accept it."""

    __slots__ = ()

    @_utils.notimplemented
    def __fspath__(self) -> str: ...


_os.PathLike.register(FsPathLike)


_FsPathLike = _ty.Union[str, FsPathLike]


class _PathnameParents(_ty.Sequence[PN]):
    """This object provides sequence-like access to the logical ancestors
    of a path.  Don't try to construct it yourself."""

    __slots__ = ("_path", "_segments")

    def __init__(self, path: PN):
        self._path = path
        segments = path.segments
        while segments and not segments[-1]:
            segments = segments[:-1]
        self._segments = segments

    def __len__(self):
        return len(self._segments)

    @_ty.overload
    def __getitem__(self, idx: slice) -> tuple[PN]: ...
    @_ty.overload
    def __getitem__(self, idx: int) -> PN: ...
    def __getitem__(self, idx: int | slice) -> tuple[PN] | PN:
        if isinstance(idx, slice):
            return tuple(self[i] for i in range(*idx.indices(len(self))))

        if idx >= len(self) or idx < -len(self):
            raise IndexError(idx)
        if idx < 0:
            idx += len(self)
        return self._path.with_segments(*self._segments[: -idx - 1])

    def __repr__(self):
        return "<{}.parents>".format(type(self._path).__name__)


class Pathname(FsPathLike, _ty.Generic[_P]):
    """Base class for manipulating paths without I/O."""

    __slots__ = ()

    @property
    def _is_case_sensitive(self) -> bool:
        return True

    @_abc.abstractmethod
    def as_uri(self) -> str: ...

    @property
    def name(self) -> str:
        """The final path component, if any."""
        segments = self.segments
        return "" if not segments else segments[-1]

    @property
    def suffix(self) -> str:
        """
        The final component's last suffix, if any.

        This includes the leading period. For example: '.txt'
        """
        name = self.name
        i = name.rfind(".")
        if 0 < i < len(name) - 1:
            return name[i:]
        else:
            return ""

    @property
    def suffixes(self):
        """
        A list of the final component's suffixes, if any.

        These include the leading periods. For example: ['.tar', '.gz']
        """
        name = self.name
        if name.endswith("."):
            return []
        name = name.lstrip(".")
        return ["." + suffix for suffix in name.split(".")[1:]]

    @property
    def stem(self):
        """The final path component, minus its last suffix."""
        name = self.name
        i = name.rfind(".")
        if 0 < i < len(name) - 1:
            return name[:i]
        else:
            return name

    @property
    @_abc.abstractmethod
    def segments(self) -> _ty.Sequence[str]: ...

    @property
    @_abc.abstractmethod
    def parts(self) -> _P: ...

    @_abc.abstractmethod
    def with_segments(self, *segments: str) -> _ty.Self: ...

    def with_name(self, name: str) -> _ty.Self:
        if not self.name:
            raise ValueError("%r has an empty name" % (self,))
        return self.with_segments(*self.segments[:-1], name)

    def with_stem(self, stem: str) -> _ty.Self:
        """Return a new path with the stem changed."""
        return self.with_name(stem + self.suffix)

    def with_suffix(self, suffix: str) -> _ty.Self:
        name = self.name
        if suffix and not suffix.startswith(".") or suffix == ".":
            raise ValueError("Invalid suffix %r" % (suffix))
        if not name:
            raise ValueError("%r has an empty name" % (self,))
        old_suffix = self.suffix
        if not old_suffix:
            name = name + suffix
        else:
            name = name[: -len(old_suffix)] + suffix
        return self.with_name(name)

    @_abc.abstractmethod
    def relative_to(self, other: _ty.Self | str) -> _ty.Self:
        """Return the relative path to another path identified by the passed
        arguments.  If the operation is not possible (because this is not
        related to the other path), raise ValueError.
        """
        ...

    def is_relative_to(self, other: _ty.Self | str):
        """Return True if the path is relative to another path or False."""
        cls = type(self)
        other = other if isinstance(other, cls) else cls(self, other)
        return other == self or other in self.parents

    def __truediv__(self, key: _ty.Self | str) -> _ty.Self:
        try:
            return type(self)(self, key)
        except (TypeError, NotImplementedError) as _err:
            return NotImplemented

    def joinpath(self, *args: str | _ty.Self) -> _ty.Self:
        """Combine this path with one or more paths/segments."""
        return type(self)(self, *args)

    @property
    def root(self) -> str:
        """The root of the path, if any (e.g. "/"). See `docs/divergences.md`
        for how this is derived generically vs. LocalPath's real one."""
        segments = self.segments
        return "/" if segments and not segments[0] else ""

    @property
    def drive(self) -> str:
        """No generic concept of a drive; always "". LocalPath gets a real
        one from pathlib on Windows."""
        return ""

    @property
    def anchor(self) -> str:
        """`drive + root`."""
        return self.drive + self.root

    @property
    @_abc.abstractmethod
    def parent(self) -> _ty.Self:
        """The logical parent of the path."""

    @property
    def parents(self) -> _ty.Sequence[_ty.Self]:
        return _PathnameParents(self)

    @_utils.notimplemented
    def is_absolute(self) -> bool:
        """True if the path is absolute"""
        ...

    def match(self, path_pattern: str | _re.Pattern, *, case_sensitive=None):
        """
        Return True if this path matches the given pattern.
        """
        if case_sensitive is None:
            case_sensitive = self._is_case_sensitive
        # as_posix(), not str(self): str() of a Uri includes scheme/host.
        path = self.as_posix()
        if not isinstance(path_pattern, _re.Pattern):
            path_pattern = _glob.compile_pattern(path_pattern, case_sensitive)
        return path_pattern.match(path) is not None

    def full_match(self, pattern: str, *, case_sensitive: bool = None) -> bool:
        """Return True if this path matches the glob-style `pattern`
        against the whole path (3.13 parity). Unlike match(), this isn't a
        right-anchored partial match, and "**" matches any number of path
        segments (including zero).
        """
        if case_sensitive is None:
            case_sensitive = self._is_case_sensitive
        return _glob.full_match(self.segments, pattern, case_sensitive)

    def as_posix(self) -> str:
        return "/".join(self.segments)

    def has_glob_pattern(self):
        for segment in self.segments:
            if _glob.WILDCARD_PATTERN.search(segment) != None:
                return True
        return False


PurePathLike = _ty.Union[str, Pathname]


class Path(Pathname, Chmod, Stat, BinaryOpen):
    """Base class for manipulating paths with I/O."""

    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        if cls is Path:
            from .fspath import LocalPath

            cls = LocalPath
        return Pathname.__new__(cls)

    def is_hidden(self):
        return self.name.startswith(".")

    def samefile(self, other_path: str | _ty.Self):
        """Return whether other_path is the same or not as this file, by
        comparing (st_dev, st_ino) when the backend's stat() provides them.
        Raises NotImplementedError otherwise (e.g. our own FileStat doesn't
        carry st_dev/st_ino) -- LocalPath gets a real implementation from
        pathlib.Path via MRO instead of this one.
        """
        other = other_path if isinstance(other_path, Path) else type(self)(other_path)
        st1 = self.stat()
        st2 = other.stat()
        ident1 = (getattr(st1, "st_dev", None), getattr(st1, "st_ino", None))
        ident2 = (getattr(st2, "st_dev", None), getattr(st2, "st_ino", None))
        if None in ident1 or None in ident2:
            raise NotImplementedError(
                "samefile() requires stat() to provide st_dev/st_ino"
            )
        return ident1 == ident2

    def __iter__(self):
        return self.iterdir()

    @_utils.notimplemented
    def iterdir(self) -> "_ty.Iterator[_ty.Self]":
        """Yield path objects of the directory contents.

        The children are yielded in arbitrary order, and the
        special entries '.' and '..' are not included.
        """
        ...

    def glob(
        self,
        pattern: str | _ty.Self,
        *,
        case_sensitive: bool = None,
        include_hidden: bool = False,
        recursive: bool = None,
        dironly: bool = None,
    ):
        """Iterate over this subtree and yield all existing files (of any
        kind, including directories) matching the given relative pattern.

        If `pattern` contains a "**" component, recursion is auto-enabled
        (pathlib parity). Pass `recursive=False` explicitly to disable
        recursion even when "**" is present, or `recursive=True` to force
        it for a pattern that doesn't contain "**".
        Note for remote schemes (http/sftp): a recursive glob walks the
        whole remote subtree, one request/roundtrip per directory.
        """
        if recursive is None:
            if isinstance(pattern, str):
                segments = pattern.split("/")
            elif hasattr(pattern, "segments"):
                segments = pattern.segments
            else:
                segments = ()
            recursive = _glob.RECURSIVE in segments
        yield from _glob.glob(
            self / pattern,
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
        recursive: bool = True,
        dironly: bool = None,
    ):
        """Equivalent to `glob(f"**/{pattern}", recursive=True)`."""
        yield from self.glob(
            f"**/{pattern}",
            case_sensitive=case_sensitive,
            include_hidden=include_hidden,
            recursive=recursive,
            dironly=dironly,
        )

    def walk(
        self,
        top_down=True,
        on_error: _ty.Callable[[OSError], None] = None,
        follow_symlinks=False,
    ):
        """Walk the directory tree from this directory, similar to os.walk()."""
        paths: "list[_ty.Self|tuple[_ty.Self, list[str], list[str]]]" = [self]

        while paths:
            path = paths.pop()
            if isinstance(path, tuple):
                yield path
                continue
            try:
                scandir_it = path.iterdir()
            except OSError as error:
                if on_error is not None:
                    on_error(error)
                continue

            dirnames: "list[str]" = []
            filenames: "list[str]" = []
            for entry in scandir_it:
                try:
                    stat = FileStat.from_path(entry, follow_symlink=follow_symlinks)
                    is_dir = stat.is_dir()
                except OSError:
                    # Carried over from os.path.isdir().
                    is_dir = False

                if is_dir:
                    dirnames.append(entry.name)
                else:
                    filenames.append(entry.name)

            if top_down:
                yield path, dirnames, filenames
            else:
                paths.append((path, dirnames, filenames))

            paths += [path / d for d in reversed(dirnames)]

    def touch(self, mode=0o666, exist_ok=True):
        """
        Create this file with the given access mode, if it doesn't exist.

        Raises FileExistsError if exist_ok is False and the file already
        exists (pathlib parity) instead of silently truncating it.
        """
        if exist_ok:
            if self.exists():
                return
            with self.open("w"):
                ...
        else:
            try:
                with self.open("x"):
                    ...
            except NotImplementedError:
                # _open() doesn't support "x" (optional per the mode
                # contract) -- emulate exclusivity best-effort. Small
                # TOCTOU window between the check and the open() below.
                if self.exists():
                    raise FileExistsError(self)
                with self.open("w"):
                    ...
        try:
            self.chmod(mode)
        except NotImplementedError:
            pass

    @_utils.notimplemented
    def _mkdir(self, mode: int): ...

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        """
        Create a new directory at this given path.
        """
        try:
            self._mkdir(mode)
        except FileNotFoundError:
            if not parents or self.parent == self:
                raise
            # Parents may already exist (e.g. a sibling branch created them)
            # -- mirror CPython: exist_ok=True for parents, original
            # exist_ok only for the final retry of self.
            self.parent.mkdir(parents=True, exist_ok=True)
            self.mkdir(mode, parents=False, exist_ok=exist_ok)
        except FileExistsError:
            if not exist_ok or not self.is_dir():
                raise

    @_utils.notimplemented
    def unlink(self, missing_ok=False):
        """
        Remove this file or link.
        If the path is a directory, use rmdir() instead.
        """

    @_utils.notimplemented
    def rmdir(self):
        """
        Remove this directory.  The directory must be empty.
        """

    def rm(
        self,
        /,
        recursive=False,
        missing_ok=False,
        ignore_error: bool | _ty.Callable[[Exception, _ty.Self], bool] = False,
    ):
        _onerror = lambda _err, _path: (
            ignore_error(_err, _path) if callable(ignore_error) else bool(ignore_error)
        )
        try:
            stat = FileStat.from_path(self)
            if stat is None:
                if not missing_ok:
                    raise FileNotFoundError(self)
            elif stat.is_dir():
                if recursive:
                    for child in self.iterdir():
                        child.rm(recursive=recursive, ignore_error=ignore_error)
                self.rmdir()
            else:
                self.unlink()
        except Exception as error:
            if not _onerror(error, self):
                raise

    @_utils.notimplemented
    def rename(self, target: "_ty.Self | str"): ...

    def copy(
        self,
        target: "Path | str",
        *,
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=True,
    ):
        """Copy this file's content to `target`.

        `follow_symlinks`/`preserve_metadata` are named to match CPython
        3.14's Path.copy() (added after this method); `overwrite` is our
        own extension (3.14 has no equivalent -- it always raises if the
        destination exists). `preserve_metadata` defaults to True here
        (unlike 3.14's False) to match this method's pre-existing behavior
        of always propagating st_mode; only st_mode is preserved, not
        timestamps/xattrs -- full metadata preservation is not implemented.
        """
        if isinstance(target, str):
            target = type(self)(target)
        src = self

        if target.exists():
            if target.is_dir():
                raise IsADirectoryError(target)
            if overwrite:
                target.unlink()
            else:
                raise FileExistsError(target)
        BinaryOpen.copy(src, target)

        if preserve_metadata:
            try:
                stat = src.stat(follow_symlinks=follow_symlinks)
                target.chmod(stat.st_mode)
            except NotImplementedError:
                pass

    def move(self, target: "Path|str", *, overwrite=False):
        if isinstance(target, str):
            target = type(self)(target)
        src = self
        if src is None:
            return

        if target.exists():
            if overwrite:
                target.unlink()
            else:
                raise FileExistsError(target)

        try:
            return src.rename(target)
        except NotImplementedError:
            pass

        BinaryOpen.copy(src, target)
        src.unlink()


PathLike = _ty.Union[str, Path]
