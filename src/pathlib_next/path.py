"""Object-oriented filesystem paths.

This module provides classes to represent abstract paths and concrete
paths with operations that have semantics appropriate for different
operating systems.
"""

import os as _os
import re as _re
import stat as _stat
from pathlib import _ignore_error
import typing as _ty
import abc as _abc
import io as _io
import shutil as _shutil

from . import utils as _utils
from .utils import glob as _glob
from .utils.stat import FileStatLike

P = _ty.TypeVar("P", bound="Path")
_P = _ty.TypeVar("_P")


class FsPathLike(_ty.Protocol):
    __slots__ = ()

    @_utils.notimplemented
    def __fspath__(self) -> str: ...


_os.PathLike.register(FsPathLike)


_FsPathLike = str | FsPathLike


class Pathname(FsPathLike, _ty.Generic[_P]):
    """Base class for manipulating paths without I/O."""

    __slots__ = ()

    @property
    def _is_case_sensitive(self) -> bool:
        return True

    @_abc.abstractmethod
    def as_uri(self) -> str: ...

    @property
    @_abc.abstractmethod
    def name(self) -> str: ...
    @property
    @_abc.abstractmethod
    def suffix(self) -> str: ...
    @property
    @_abc.abstractmethod
    def stem(self) -> str: ...
    @property
    @_abc.abstractmethod
    def segments(self) -> _ty.Iterable[str]: ...

    @property
    @_abc.abstractmethod
    def parts(self) -> _P: ...

    @_abc.abstractmethod
    def with_name(self, name: str) -> _ty.Self: ...

    def with_stem(self, stem: str) -> _ty.Self:
        """Return a new path with the stem changed."""
        return self.with_name(stem + self.suffix)

    @_abc.abstractmethod
    def with_suffix(self, suffix: str) -> _ty.Self: ...

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
        except (TypeError, NotImplementedError):
            return NotImplemented

    @property
    @_abc.abstractmethod
    def parent(self) -> _ty.Self:
        """The logical parent of the path."""

    @property
    def parents(self) -> _ty.Iterable[_ty.Self]:
        parent = self.parent
        if parent != self:
            yield parent
            yield from parent.parents

    @_utils.notimplemented
    def is_absolute(self) -> bool:
        """True if the path is absolute (has both a root and, if applicable,
        a drive)."""
        ...

    def match(self, path_pattern: str | _re.Pattern, *, case_sensitive=None):
        """
        Return True if this path matches the given pattern.
        """
        if case_sensitive is None:
            case_sensitive = self._is_case_sensitive
        path = str(self)
        if not isinstance(path_pattern, _re.Pattern):
            if isinstance(str, path_pattern):
                path_pattern = type(self)(path_pattern).__path__()
            path_pattern = _glob.compile_pattern(path_pattern, case_sensitive)
        return path_pattern.match(path) is not None

    @_utils.notimplemented
    def as_posix(self) -> str: ...


PurePathLike = str | Pathname


class Path(Pathname):
    """Base class for manipulating paths with I/O."""

    __slots__ = ()

    @_utils.notimplemented
    def stat(self, *, follow_symlinks=True) -> FileStatLike: ...

    def lstat(self) -> FileStatLike:
        """
        Like stat(), except if the path points to a symlink, the symlink's
        status information is returned, rather than its target's.
        """
        return self.stat(follow_symlinks=False)

    def _st_mode(self, *, follow_symlinks=True):
        try:
            return self.stat().st_mode
        except FileNotFoundError:
            return None
        except OSError as e:
            if not _ignore_error(e):
                raise
            return None
        except ValueError:
            return None

    # Convenience functions for querying the stat results
    def exists(self, *, follow_symlinks=True):
        """
        Whether this path exists.
        """
        return self._st_mode(follow_symlinks=follow_symlinks) != None

    def is_hidden(self):
        return self.name.startswith(".")

    def is_dir(self):
        """
        Whether this path is a directory.
        """
        try:
            return _stat.S_ISDIR(self._st_mode() or 0)
        except NotImplementedError:
            pass
        try:
            next(self.iterdir(), None)
            return True
        except NotADirectoryError:
            return False

        raise NotImplementedError("is_dir")

    def is_file(self):
        """
        Whether this path is a regular file (also True for symlinks pointing
        to regular files).
        """
        return _stat.S_ISREG(self._st_mode() or 0)

    def is_symlink(self):
        """
        Whether this path is a symbolic link.
        """
        return _stat.S_ISLNK(self._st_mode(follow_symlinks=True) or 0)

    def is_block_device(self):
        """
        Whether this path is a block device.
        """
        return _stat.S_ISBLK(self._st_mode() or 0)

    def is_char_device(self):
        """
        Whether this path is a character device.
        """
        return _stat.S_ISCHR(self._st_mode() or 0)

    def is_fifo(self):
        """
        Whether this path is a FIFO.
        """
        return _stat.S_ISFIFO(self._st_mode() or 0)

    def is_socket(self):
        """
        Whether this path is a socket.
        """
        return _stat.S_ISSOCK(self._st_mode() or 0)

    @_utils.notimplemented
    def samefile(self, other_path: str | _ty.Self):
        """Return whether other_path is the same or not as this file
        (as returned by os.path.samefile()).
        """
        ...

    @_utils.notimplemented
    def _open(
        self,
        mode="r",
        buffering=-1,
    ) -> _io.IOBase:
        """
        All operations should be binary
        To be used only by UriPath.open() to obtain binary stream
        """
        ...

    def open(
        self,
        mode="r",
        buffering=-1,
        encoding: str = None,
        errors: str = None,
        newline: str = None,
    ) -> _io.IOBase:
        """
        Open the file pointed by this path and return a file object, as
        the built-in open() function does.
        """
        fh = self._open(mode.replace("b", ""), buffering)
        if "b" not in mode:
            encoding = _io.text_encoding(encoding)
            fh = _io.TextIOWrapper(fh, encoding, errors, newline)
        return fh

    def read_bytes(self) -> bytes:
        """
        Open the file in bytes mode, read it, and close the file.
        """
        with self.open(mode="rb") as f:
            return f.read()

    def read_text(self, encoding: str = None, errors: str = None) -> str:
        """
        Open the file in text mode, read it, and close the file.
        """
        with self.open(mode="r", encoding=encoding, errors=errors) as f:
            return f.read()

    def write_bytes(self, data: bytes):
        """
        Open the file in bytes mode, write to it, and close the file.
        """
        # type-check for the buffer interface before truncating the file
        view = memoryview(data)
        with self.open(mode="wb") as f:
            return f.write(view)

    def write_text(
        self, data: str, encoding: str = None, errors: str = None, newline: str = None
    ):
        """
        Open the file in text mode, write to it, and close the file.
        """
        if not isinstance(data, str):
            raise TypeError("data must be str, not %s" % data.__class__.__name__)
        with self.open(
            mode="w", encoding=encoding, errors=errors, newline=newline
        ) as f:
            return f.write(data)

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
        recursive: bool = False,
        dironly: bool = None,
    ):
        """Iterate over this subtree and yield all existing files (of any
        kind, including directories) matching the given relative pattern.
        """
        yield from _glob.glob(
            self / pattern,
            case_sensitive=case_sensitive,
            include_hidden=include_hidden,
            recursive=recursive,
            dironly=dironly,
        )

    def rglob(
        self,
        pattern: str | _ty.Self,
        *,
        case_sensitive: bool = None,
        include_hidden: bool = False,
        dironly: bool = None,
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
                    is_symlink = entry.is_symlink()
                    is_dir = entry.is_dir()
                except OSError:
                    # Carried over from os.path.isdir().
                    is_dir = False

                if is_dir and (follow_symlinks or not is_symlink):
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
        """

        if exist_ok:
            if self.exists():
                return

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
            self.parent.mkdir(parents=True, exist_ok=True)
            self.mkdir(mode, parents=False, exist_ok=exist_ok)
        except OSError:
            # Cannot rely on checking for EEXIST, since the operating system
            # could give priority to other errors like EACCES or EROFS
            if not exist_ok or not self.is_dir():
                raise

    @_utils.notimplemented
    def chmod(self, mode: int, *, follow_symlinks=True):
        """
        Change the permissions of the path, like os.chmod().
        """
        ...

    def lchmod(self, mode: int):
        """
        Like chmod(), except if the path points to a symlink, the symlink's
        permissions are changed, rather than its target's.
        """
        self.chmod(mode, follow_symlinks=False)

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
            ignore_error if not callable(ignore_error) else ignore_error
        )
        try:
            if not self.exists():
                if missing_ok:
                    return
                raise FileNotFoundError(self)
            if self.is_dir():
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

    def copy(self, target: "Path | str", *, overwrite=False):
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

        with target.open("wb") as output, src.open("rb") as input:
            _shutil.copyfileobj(input, output)

        try:
            stat = src.stat()
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

        src.copy(target)
        src.unlink()


PathLike = str | Path
