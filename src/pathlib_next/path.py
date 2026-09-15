"""Object-oriented filesystem paths.

This module provides classes to represent abstract paths and concrete
paths with operations that have semantics appropriate for different
operating systems.
"""

from __future__ import annotations

import abc as _abc
import errno as _errno
import os as _os
import re as _re
import sys as _sys
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

    __slots__ = ("_path", "_anchored", "_segments")

    def __init__(self, path: PN):
        self._path = path
        segments = tuple(path.segments)
        # A leading "" marks the root of an absolute path. It is the anchor,
        # not an ancestor of its own: counting it made MemPath("/a/b").parents
        # ['/a', '', ''] -- one entry too many, and the root spelled as the
        # relative empty path (pathlib: ['/a', '/']).
        self._anchored = bool(segments) and segments[0] == ""
        if self._anchored:
            segments = segments[1:]
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
        kept = self._segments[: -idx - 1]
        if not self._anchored:
            return self._path.with_segments(*kept)
        # ("", "") is the root in with_segments' "/"-joined spelling.
        return self._path.with_segments("", *(kept or ("",)))

    def __repr__(self):
        return "<{}.parents>".format(type(self._path).__name__)


class Pathname(FsPathLike, _ty.Generic[_P]):
    """Base class for manipulating paths without I/O."""

    __slots__ = ()

    @property
    def _is_case_sensitive(self) -> bool:
        return True

    @_abc.abstractmethod
    def as_uri(self) -> str:
        """Return the path as a URI string."""
        ...

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
    def segments(self) -> _ty.Sequence[str]:
        """The sequence of path component strings."""
        ...

    @property
    @_abc.abstractmethod
    def parts(self) -> _P:
        """The individual components/parts of the path."""
        ...

    @_abc.abstractmethod
    def with_segments(self, *segments: str) -> _ty.Self:
        """Construct a same-type path instance from new segments."""
        ...

    def with_name(self, name: str) -> _ty.Self:
        """Return a new path with the name changed.

        Raises ValueError for an empty name, ".", or a name containing "/",
        as pathlib does. Without the check a name such as "../../etc/passwd"
        or "x/y" was spliced in verbatim, so a caller relying on pathlib's
        validation of an untrusted file name got a path outside the
        directory. A bare ".." is accepted, also as pathlib does; use
        `utils.is_safe_child_name()` to reject it. `with_stem()`/
        `with_suffix()` validate their result through here.
        """
        if not name or "/" in name or name == ".":
            raise ValueError("Invalid name %r" % (name,))
        if not self.name:
            raise ValueError("%r has an empty name" % (self,))
        return self.with_segments(*self.segments[:-1], name)

    def with_stem(self, stem: str) -> _ty.Self:
        """Return a new path with the stem changed (validated like
        `with_name()`)."""
        return self.with_name(stem + self.suffix)

    def with_suffix(self, suffix: str) -> _ty.Self:
        """Return a new path with the suffix changed or added."""
        name = self.name
        if (
            suffix
            and not suffix.startswith(".")
            or (suffix == "." and _sys.version_info < (3, 14))
        ):
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
        # with_segments(other), NOT cls(self, other): joining `other` under
        # `self` first turned `MemPath("a/b").is_relative_to("a")` into a
        # comparison against "a/b/a" and answered False, while the object
        # form of the same call answered True. CPython parses `other`
        # standalone (`self.with_segments(other)`) and so do we -- via
        # with_segments rather than the bare constructor so per-instance
        # state a subclass carries (MemPath's backend) survives.
        other = other if isinstance(other, cls) else self.with_segments(other)
        return other == self or other in self.parents

    def __eq__(self, other: object) -> bool:
        """Compare by (exact type, segments).

        The ABC previously defined no equality at all, so every pure
        subclass that didn't hand-write one -- including `MemPath`, the
        documented reference exemplar -- compared by identity. That made
        `is_relative_to()` (which decides via `==`) silently return False
        for every subclass, and broke paths as dict keys or set members.
        `_BaseFSPathname`/`LocalPath` are unaffected: `pathlib.PurePath`
        precedes `Pathname` in their MRO and keeps its own `__eq__`, as
        does `Uri`, which defines one.
        """
        if type(self) is not type(other):
            return NotImplemented
        return tuple(self.segments) == tuple(other.segments)

    def __hash__(self) -> int:
        return hash((type(self), tuple(self.segments)))

    def __truediv__(self, key: _ty.Self | str) -> _ty.Self:
        try:
            return type(self)(self, key)
        except (TypeError, NotImplementedError):
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
        """An immutable sequence providing access to the logical ancestors of the path."""
        return _PathnameParents(self)

    @_utils.notimplemented
    def is_absolute(self) -> bool:
        """True if the path is absolute"""
        ...

    def _match_parts(self) -> tuple[bool, list[str]]:
        """`(anchored, names)`: the path as `match()` sees it -- whether it
        is rooted, and its names with empty and "." segments dropped, the
        way `PurePosixPath` parses a string."""
        segments = self.segments
        anchored = bool(segments) and segments[0] == ""
        return anchored, [s for s in segments if s and s != "."]

    def match(self, path_pattern: str | _re.Pattern, *, case_sensitive=None):
        """
        Return True if this path matches the given glob-style pattern.

        pathlib's semantics on the running interpreter: a relative pattern
        matches the last N segments (from the right), an absolute pattern
        must match the whole path, and each segment is matched on its own,
        so `*` never crosses a "/". "**" acts like "*" (use `full_match()`
        for recursive matching). An empty pattern raises ValueError.

        A compiled `re.Pattern` cannot be split into segments; it is matched
        against the whole path string instead ("/"-joined segments, so a
        `Uri`'s scheme and host are never part of it).
        """
        import fnmatch as _fnmatch

        if case_sensitive is None:
            case_sensitive = self._is_case_sensitive
        anchored, names = self._match_parts()
        if isinstance(path_pattern, _re.Pattern):
            path = ("/" if anchored else "") + "/".join(names)
            return path_pattern.match(path) is not None
        if isinstance(path_pattern, Pathname):
            path_pattern = "/".join(path_pattern.segments)
        elif not isinstance(path_pattern, str):
            path_pattern = _os.fspath(path_pattern)
        pattern_anchored = path_pattern.startswith("/")
        pattern_names = [s for s in path_pattern.split("/") if s and s != "."]
        if not pattern_names and not pattern_anchored:
            raise ValueError("empty pattern")
        if pattern_anchored and not (anchored and len(names) == len(pattern_names)):
            return False
        # The root counts as one more part a relative pattern can reach.
        path_parts = len(names) + anchored
        if len(pattern_names) > path_parts:
            return False
        flags = 0 if case_sensitive else _re.IGNORECASE
        for index, pattern in enumerate(reversed(pattern_names)):
            if index == len(names):
                # A relative pattern as long as the path reaches its root.
                # 3.12+ compares it like any other part, and no wildcard
                # matches a separator; 3.9-3.11 fnmatch the root string, so
                # "*" and "?" match it there.
                if _sys.version_info >= (3, 12):
                    return False
                return _re.match(_fnmatch.translate(pattern), "/", flags) is not None
            name = names[len(names) - 1 - index]
            if _re.match(_fnmatch.translate(pattern), name, flags) is None:
                return False
        return True

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
        """Return the string representation of the path with forward slashes."""
        return "/".join(self.segments)

    def has_glob_pattern(self):
        """Return True if any of the path segments contain glob wildcards."""
        for segment in self.segments:
            if _glob.WILDCARD_PATTERN.search(segment) is not None:
                return True
        return False


PurePathLike = _ty.Union[str, Pathname]


# Operations whose implementation must come from pathlib_next even when a
# concrete `pathlib` class sits ahead of us in a subclass's MRO. See
# `Path.__init_subclass__` for why this is needed and how it is applied.
#
# Two different, opposite failure modes motivate this list:
#   * NEW stdlib overriding us: `copy`/`move` landed in CPython 3.14 and
#     expect the private `_copy_from` protocol, so a downstream
#     `class X(PosixPathname, Path)` (or any class mixing a concrete
#     `pathlib` path) either crashes with
#     `AttributeError: ... has no attribute '_copy_from'` on non-local
#     backends, or -- worse -- SILENTLY succeeds on local-backed classes
#     with stdlib's different metadata semantics (it preserves timestamps,
#     ours preserves st_mode only). That makes mtime-based syncs converge
#     on 3.14 and never converge on <=3.13.
#   * OLD stdlib lacking our keywords: `exists(follow_symlinks=)` is 3.12+
#     and `read_text`/`write_text`'s `newline=` is 3.13+ in CPython, and
#     `rglob`'s `include_hidden=`/`recursive=`/`dironly=` extensions never
#     existed there, so on the 3.9 floor the stdlib implementation rejects
#     keywords this library's protocols promise. `symlink_to`'s `force=`
#     is the same shape: no stdlib version has ever accepted it, so
#     without this guard `LocalPath().symlink_to(t, force=True)` raises
#     TypeError while every other backend honors it.
#
# `glob`/`walk`/`_scandir` are deliberately absent: `LocalPath` overrides
# them itself (with local-specific behavior that must be kept), so they are
# already pathlib_next-owned wherever it matters.
_OPERATION_NAMES = (
    "copy",
    "move",
    "exists",
    "rglob",
    "read_text",
    "write_text",
    "symlink_to",
)


class Path(Pathname, Chmod, Stat, BinaryOpen):
    """Base class for manipulating paths with I/O."""

    __slots__ = ()

    def __init_subclass__(cls, **kwargs):
        """Guarantee pathlib_next operation precedence in every subclass.

        Concrete path classes are routinely built by mixing a `pathlib`
        class with this one -- our own `LocalPath` does it, and the
        documented downstream recipe (`class X(PosixPathname, Path)`) does
        it transitively. Python's MRO then resolves a name to whichever
        base declares it first, which for those classes can be `pathlib`
        rather than `pathlib_next` -- and which one wins changes with the
        interpreter version, because stdlib `pathlib` keeps gaining and
        changing methods (see `_OPERATION_NAMES`).

        Rather than making every downstream implementer rediscover this and
        hand-write forwarding methods, re-assert our implementations here
        for any subclass that would otherwise inherit a non-pathlib_next
        one. A subclass (or an intermediate mixin) that defines the method
        *itself* is always left alone -- this only displaces implementations
        coming from outside this library.
        """
        super().__init_subclass__(**kwargs)
        for name in _OPERATION_NAMES:
            # A class that defines the operation in its OWN body is always
            # authoritative -- never displace a deliberate override (this
            # also covers `LocalPath.copy`/`move`'s explicit routing).
            if name in vars(cls):
                continue
            # Find which class in the MRO actually supplies the inherited
            # implementation. Checking the resolved function's `__module__`
            # is not enough: a downstream mixin may legitimately define the
            # method in its own module, and that must be honored too.
            owner = next((base for base in cls.__mro__[1:] if name in vars(base)), None)
            if owner is None:
                continue
            # Only stdlib `pathlib` is displaced. Anything else -- a
            # downstream mixin, a user base class, or pathlib_next itself --
            # is a deliberate implementation and is left alone. Matching on
            # stdlib specifically (rather than "not pathlib_next") is what
            # keeps this guard from hijacking third-party code.
            owner_module = getattr(owner, "__module__", "") or ""
            if owner_module != "pathlib" and not owner_module.startswith("pathlib."):
                continue
            ours = getattr(Path, name, None)
            if ours is None:
                continue
            setattr(cls, name, ours)

    def __new__(cls, *args, **kwargs):
        if cls is Path:
            from .fspath import LocalPath

            # LocalPath doesn't define its own __new__, so LocalPath.__new__
            # resolves via *its* MRO to the real pathlib.Path.__new__
            # (WindowsPath/PosixPath precede our Path in LocalPath's bases,
            # see fspath.py) -- which is what actually parses `args` into
            # _drv/_root/_parts on Python <3.12 (3.12+ does this in
            # PurePath.__init__ instead, called separately afterward
            # regardless). Calling `Pathname.__new__(cls)` here instead used
            # to skip that parsing entirely and silently drop `args`,
            # leaving a blank instance -- masked on 3.12+ because __init__
            # does the real work there, but crashed on 3.9-3.11 the moment
            # any pathlib internal (e.g. `/`) touched the missing state.
            return LocalPath.__new__(LocalPath, *args, **kwargs)
        return Pathname.__new__(cls)

    def is_hidden(self):
        """Return True if the final path component is a hidden file/directory."""
        return self.name.startswith(".")

    def samefile(self, other_path: str | _ty.Self):
        """Return whether other_path is the same or not as this file, by
        comparing (st_dev, st_ino) when the backend's stat() provides them.
        Raises NotImplementedError otherwise (e.g. our own FileStat doesn't
        carry st_dev/st_ino) -- LocalPath gets a real implementation from
        pathlib.Path via MRO instead of this one.
        """
        other = (
            other_path
            if isinstance(other_path, Path)
            else self._coerce_target(other_path)
        )
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

    def _scandir(self) -> "_ty.Iterator[_ty.Tuple[str, _ty.Optional[FileStat]]]":
        """Yield (name, stat_or_None) for each directory entry. Used by
        `walk()`/`glob()` instead of `iterdir()` so schemes whose listing
        call already returns metadata (HttpPath, DavPath, SftpPath, FtpPath,
        S3Path) can answer `is_dir()` on the results without a further
        round trip per entry. Default: falls back to `iterdir()` + one
        `stat()` per child (no round-trip savings, but no subclass is
        required to implement this) -- see `docs/guides/extending.md`.
        """
        for entry in self.iterdir():
            try:
                # follow_symlink=False to match walk()'s own default -- an
                # explicit walk(follow_symlinks=True) always re-stats each
                # entry itself regardless of what's yielded here.
                stat = FileStat.from_path(entry, follow_symlink=False)
            except OSError:
                stat = None
            yield entry.name, stat

    def glob(
        self,
        pattern: str | _ty.Self,
        *,
        case_sensitive: bool = None,
        include_hidden: bool = True,
        recursive: bool = None,
        dironly: bool = None,
        recurse_symlinks: bool = False,
    ):
        """Iterate over this subtree and yield all existing files (of any
        kind, including directories) matching the given relative pattern.

        Pathlib semantics: hidden entries are matched (`include_hidden=False`
        filters them out and skips hidden directories), a trailing separator
        selects directories only, "**" never descends into a directory
        symlink, and a trailing "**" also selects files on Python 3.13+
        (directories only before). A missing or non-directory parent selects
        nothing. An empty pattern raises `ValueError`, an absolute one
        `glob.NonRelativePatternError` (a `NotImplementedError` and a
        `ValueError`). `recurse_symlinks=True` is not supported.

        A "**" component auto-enables recursion. Pass `recursive=False`
        explicitly to treat "**" as a plain "*" instead.
        Note for remote schemes (http/sftp): a recursive glob walks the
        whole remote subtree, one request/roundtrip per directory.
        """
        if recurse_symlinks:
            raise NotImplementedError("glob(recurse_symlinks=True)")
        # Validates eagerly (like pathlib 3.13+); the returned selection is
        # lazy. The pattern is never joined onto self: `self / pattern` let an
        # absolute pattern escape self and re-parsed "?" as a URI query.
        parts, trailing_sep = _glob.parse_pattern(pattern)
        if recursive is None:
            recursive = _glob.RECURSIVE in parts
        return _glob.select(
            self,
            parts,
            dironly=trailing_sep if dironly is None else dironly,
            recursive=recursive,
            include_hidden=include_hidden,
            case_sensitive=case_sensitive,
        )

    def rglob(
        self,
        pattern: str,
        *,
        case_sensitive: bool = None,
        include_hidden: bool = True,
        recursive: bool = True,
        dironly: bool = None,
        recurse_symlinks: bool = False,
    ):
        """Equivalent to `glob(f"**/{pattern}", recursive=True)`."""
        if not (isinstance(pattern, str) and not pattern):
            # Reject an absolute pattern before "**/" hides its anchor;
            # glob() validates without listing anything.
            self.glob(pattern, recursive=False)
        return self.glob(
            f"**/{pattern}",
            case_sensitive=case_sensitive,
            include_hidden=include_hidden,
            recursive=recursive,
            dironly=dironly,
            recurse_symlinks=recurse_symlinks,
        )

    def walk(
        self,
        top_down=True,
        on_error: _ty.Callable[[OSError], None] = None,
        follow_symlinks=False,
    ):
        """Walk the directory tree from this directory, similar to os.walk().

        Uses `_scandir()` rather than `iterdir()` + a `stat()` per entry --
        for schemes whose listing already carries type metadata (HTTP/DAV
        indexes, SFTP `listdir_attr`, FTP MLSD, S3 list pages), this turns a
        remote-tree walk from O(entries) round trips into O(dirs). The
        pre-seeded stat is trusted only when `follow_symlinks` is False
        (matching `walk()`'s own default and the lstat-like semantics of
        those listing calls); an explicit `follow_symlinks=True` always
        re-`stat()`s each entry so a symlink is still resolved.
        """
        paths: "list[_ty.Self|tuple[_ty.Self, list[str], list[str]]]" = [self]

        while paths:
            path = paths.pop()
            if isinstance(path, tuple):
                yield path
                continue
            try:
                # `_scandir()` is a generator -- listing this directory may
                # not actually happen (and so may not raise) until the first
                # `next()`, not at this call. Materializing it here (rather
                # than iterating lazily below) keeps any such error inside
                # this try, matching the on_error contract regardless of
                # whether a given implementation happens to fail eagerly or
                # lazily.
                entries = list(path._scandir())
            except OSError as error:
                if on_error is not None:
                    on_error(error)
                continue

            dirnames: "list[str]" = []
            filenames: "list[str]" = []
            for name, stat in entries:
                try:
                    if stat is None or follow_symlinks:
                        stat = FileStat.from_path(
                            path / name, follow_symlink=follow_symlinks
                        )
                    is_dir = stat.is_dir() if stat is not None else False
                except OSError:
                    # Carried over from os.path.isdir().
                    is_dir = False

                if is_dir:
                    dirnames.append(name)
                else:
                    filenames.append(name)

            if top_down:
                yield path, dirnames, filenames
            else:
                paths.append((path, dirnames, filenames))

            paths += [path / d for d in reversed(dirnames)]

    def touch(self, mode=None, exist_ok=True):
        """
        Create this file, if it doesn't exist.

        Raises FileExistsError if exist_ok is False and the file already
        exists (pathlib parity). An existing file is never truncated.

        Differences from `pathlib.Path.touch`, which creates through
        `os.open()` (so the process umask applies) and bumps an existing
        file's mtime:

        - `mode` is applied with `chmod()` only when passed explicitly, and
          then verbatim: a remote server's umask is unknowable. The default
          (`None`) leaves a new file with the permissions the backend gives
          it, instead of chmod'ing it to a world-writable 0o666.
        - An existing file's mtime is left unchanged: the generic protocol
          has no timestamp primitive, and rewriting the content to bump it
          is not a touch. `LocalPath` and `FileUri` use pathlib's own touch.
        """
        # stat() directly, not exists(): exists() reads *any* OSError (a
        # timeout, a dropped connection) as "missing", which let a transient
        # failure fall through to a truncating open("w").
        try:
            self.stat()
        except FileNotFoundError:
            pass
        else:
            if not exist_ok:
                raise FileExistsError(self)
            return
        try:
            with self.open("x"):
                ...
        except FileExistsError:
            # Created since the stat() above: an existing file, not ours to
            # truncate.
            if not exist_ok:
                raise
            return
        except NotImplementedError:
            # _open() doesn't support "x" (optional per the mode contract),
            # so the stat() above is the only guard before the truncating
            # "w" -- a small TOCTOU window.
            with self.open("w"):
                ...
        if mode is not None:
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
        """Remove this file or directory, optionally recursively and ignoring errors."""
        # Same bool-or-callable normalization as copy()/PathSyncer, via the
        # shared helper. A supplied callable keeps rm()'s own `(error, path)`
        # arity -- arities differ per call site by design, see the helper.
        _onerror = _utils.as_error_handler(ignore_error)

        def _handle(error, path):
            if not _onerror(error, path):
                raise error

        def _scan_entries(path):
            for entry in path._scandir():
                if isinstance(entry, tuple) and len(entry) == 2:
                    yield entry
                    continue
                try:
                    stat = FileStat.from_stat(entry.stat(follow_symlinks=False))
                except OSError:
                    stat = None
                yield entry.name, stat

        def _remove_tree(path):
            try:
                entries = list(_scan_entries(path))
            except Exception as error:
                _handle(error, path)
                return

            for name, child_stat in entries:
                child = path / name
                try:
                    if child_stat is None:
                        child_stat = FileStat.from_path(child, follow_symlink=False)
                    if child_stat is not None and child_stat.is_dir():
                        if child._is_junction_link():
                            child.rmdir()
                        else:
                            _remove_tree(child)
                    else:
                        child.unlink()
                except Exception as error:
                    _handle(error, child)

            try:
                path.rmdir()
            except Exception as error:
                _handle(error, path)

        try:
            stat = FileStat.from_path(self, follow_symlink=False)
        except Exception as error:
            _handle(error, self)
            return
        if stat is None:
            if not missing_ok:
                _handle(FileNotFoundError(self), self)
        elif stat.is_dir():
            if recursive and not self._is_junction_link():
                _remove_tree(self)
            else:
                try:
                    self.rmdir()
                except Exception as error:
                    _handle(error, self)
        else:
            try:
                self.unlink()
            except Exception as error:
                _handle(error, self)

    def _coerce_target(self, target: str) -> "Path":
        """Turn a `str` destination (`copy()`, `move()`, `samefile()`) into a
        path on the same backend. The default keeps per-instance state (a
        `MemPath`'s in-memory filesystem) via `with_segments()`; the bare
        constructor used before gave every str destination a fresh, empty
        backend, so `move("/c.txt")` wrote there and then deleted the source.
        `UriPath` overrides this to parse the string as a URI."""
        return self.with_segments(target)

    def _rename_compatible(self, target: "Path") -> bool:
        """Whether `rename()` may be attempted onto `target` at all. `move()`
        skips straight to copy + delete when it is not. Default True: URI
        schemes decide per target in `rename()` (raising NotImplementedError);
        `LocalPath` requires a local target, since `os.rename()` would
        otherwise take a remote path's `__fspath__()` as a local name."""
        return True

    def _is_junction_link(self) -> bool:
        """Whether this path is a directory *link* that a non-following stat
        still reports as a directory (a Windows junction).

        `rm(recursive=True)` removes such an entry with `rmdir()` instead of
        descending into it: its contents belong to the link's target, outside
        the tree being removed. Default False; `LocalPath` answers for real.
        """
        return False

    @_utils.notimplemented
    def rename(self, target: "_ty.Self | str"):
        """Rename this file or directory to the given target."""
        ...

    @_utils.notimplemented
    def _symlink_to(
        self, target: "_ty.Self", target_is_directory: bool = False
    ) -> None:
        """Create a symlink at this path pointing at `target`.

        The backend primitive behind `symlink_to()`, in the same shape as
        `_mkdir`/`_open`: implement only this, and the generic conveniences
        (`force=`) come for free. `target` has already been normalized to a
        path object of this class by `symlink_to()` -- accepting `str` and
        turning it into a path is the wrapper's job, not the primitive's.

        Implementations take the raw target string from the path object the
        way their own backend needs it (`Uri.path` for wire protocols,
        `os.fspath()`/`as_posix()` locally); it never carries a scheme or
        host prefix, and a relative target stays relative.
        """
        ...

    def _symlink_target(self, target: "_ty.Self | str") -> "_ty.Self":
        """Normalize a `symlink_to()` target argument to a path object.

        A `str` target is the **literal link target** -- whatever it says
        is what gets stored, verbatim and unresolved, exactly as
        `pathlib.Path.symlink_to()` does. Override this wherever
        `type(self)(str)` would reinterpret the string instead of taking
        it literally (`UriPath` does; see `UriPath._symlink_target`).
        """
        return type(self)(target) if isinstance(target, str) else target

    def symlink_to(
        self,
        target: "_ty.Self | str",
        target_is_directory: bool = False,
        *,
        force: bool = False,
    ) -> None:
        """Make this path a symlink pointing to `target`.

        Signature-compatible with `pathlib.Path.symlink_to()`; `force` is a
        pathlib_next extension (see `docs/divergences.md`).

        With `force=True`, an existing entry at *this* path is removed
        first. No filesystem or transport offers an atomic "replace a
        symlink" operation, so this is an unlink-then-symlink sequence and
        is therefore **not** atomic: between the two steps the path does not
        exist, and a concurrent writer can win the race. It is a
        convenience, not a locking primitive.

        Only a non-directory entry is removed -- an existing *directory* at
        the link path is left alone and the underlying `FileExistsError`
        (or the backend's equivalent) propagates. Silently deleting a
        directory tree is never what `force=` on a symlink call is asking
        for.
        """
        # Normalize a str target to a path object, so the primitive only
        # ever handles one type. Routed through `_symlink_target()` rather
        # than inlining `type(self)(target)`: for a URI-backed path that
        # constructor re-parses the string as URI syntax, which silently
        # truncated a link target at a "?"/"#" and percent-decoded it (see
        # `UriPath._symlink_target`).
        target = self._symlink_target(target)
        if force:
            try:
                self.unlink(missing_ok=True)
            except (IsADirectoryError, PermissionError, OSError) as error:
                # A directory at the link path (POSIX: IsADirectoryError;
                # Windows and several remote backends: PermissionError or a
                # plain OSError) is not something force= should remove.
                # Let the symlink attempt below raise the error that
                # actually describes the conflict.
                if not self.is_dir():
                    raise error
        return self._symlink_to(target, target_is_directory)

    def copy(
        self,
        target: "Path | str",
        *,
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=True,
        recursive=False,
        ignore_error=None,
        progress: "_ty.Callable[[_ty.Self, int, _ty.Optional[int]], None]" = None,
    ):
        """Copy this file's content to `target`.

        `follow_symlinks`/`preserve_metadata` are named to match CPython
        3.14's Path.copy() (added after this method); `overwrite` is our
        own extension (3.14 has no equivalent -- it always raises if the
        destination exists). `preserve_metadata` defaults to True here
        (unlike 3.14's False) to match this method's pre-existing behavior
        of always propagating st_mode; only st_mode is preserved, not
        timestamps/xattrs -- full metadata preservation is not implemented.
        `ignore_error` accepts a bool or a callable, matching `Path.rm()`'s
        bool-or-callable contract. `True` ignores every error; `False` and
        `None` (the default) fail on the first error. A callable is invoked
        as `ignore_error(error)` -- this call site's own arity -- and, as it
        always has here, is a *notification* hook: the error is suppressed
        regardless of what it returns, so handlers like `errors.append`
        (returning None) keep working. Only errors from *child* copies
        during a `recursive=True` copy are routed here.

        `progress`, when given, is called as `progress(path, bytes_copied,
        total_size)` for every chunk written during each *file* copy (`path`
        is the source `Path` being streamed -- `self` for a single-file
        copy, or the relevant child during a `recursive=True` copy).
        `bytes_copied` increases monotonically per file and reaches
        `total_size` (or `None` if the size couldn't be determined) at the
        end of that file. Directories themselves don't get a progress call
        (only the files inside them do). With `progress=None` (the
        default), behavior is unchanged -- no per-chunk overhead. Native
        backend transfers that bypass the generic streaming copy (e.g.
        `SftpPath`'s asyncssh concurrent fan-out) do not invoke `progress`;
        see `docs/divergences.md`'s "Deliberate extensions" section.
        """
        if isinstance(target, str):
            target = self._coerce_target(target)
        src = self

        if recursive and src.is_dir():
            if target.exists():
                if not target.is_dir():
                    raise FileExistsError(target)
                if not overwrite:
                    raise FileExistsError(target)
            else:
                target.mkdir()
            for child in src.iterdir():
                try:
                    child.copy(
                        target / child.name,
                        overwrite=overwrite,
                        follow_symlinks=follow_symlinks,
                        preserve_metadata=preserve_metadata,
                        recursive=True,
                        ignore_error=ignore_error,
                        progress=progress,
                    )
                except Exception as e:
                    # A callable stays a notify-and-suppress hook (its return
                    # value was never consulted here, and callers such as
                    # `errors.append` rely on that). Bools are new: True
                    # suppresses, False/None raise -- matching rm()'s bool
                    # semantics without changing the callable contract.
                    if callable(ignore_error):
                        ignore_error(e)
                    elif not ignore_error:
                        raise
            return

        if _same_file(src, target):
            raise OSError(
                _errno.EINVAL, "Source and target are the same file", str(target)
            )
        target_exists = target.exists()
        if target_exists:
            if target.is_dir():
                raise IsADirectoryError(target)
            if not overwrite:
                raise FileExistsError(target)

        # Open the source before the target is touched at all: a missing or
        # unreadable source must leave an existing target intact and must not
        # leave a new empty one behind.
        with src.open("rb") as input:
            if target_exists:
                target.unlink()
            created = False
            try:
                with target.open("wb") as output:
                    created = True
                    BinaryOpen._copy_stream(
                        src,
                        input,
                        output,
                        progress=(
                            None
                            if progress is None
                            else lambda copied, total: progress(src, copied, total)
                        ),
                    )
            except BaseException:
                # A half-written target is not a copy of anything; do not
                # leave it looking like one.
                if created:
                    try:
                        target.unlink(missing_ok=True)
                    except Exception:
                        pass
                raise

        if preserve_metadata:
            try:
                stat = src.stat(follow_symlinks=follow_symlinks)
                # Only a mode the source backend actually reported is
                # metadata. FileStat's placeholder (0o444 for a file) is not:
                # applying it made every copy from MemPath/HTTP/S3/... a
                # read-only file that a re-copy or re-sync could not replace.
                if getattr(stat, "mode_known", True) and stat.st_mode:
                    target.chmod(stat.st_mode)
            except NotImplementedError:
                pass

    def move(self, target: "Path|str", *, overwrite=False):
        """Move this file or directory to target, falling back to copy+unlink/rm if rename is unsupported."""
        if isinstance(target, str):
            target = self._coerce_target(target)
        src = self
        # rename() only makes sense between paths the same backend can see.
        native = src._rename_compatible(target)

        # Everything that can fail cheaply is checked before the target is
        # touched: a missing source, or a file onto a directory, used to
        # delete the target and only then raise.
        src_stat = FileStat.from_path(src, follow_symlink=False)
        if src_stat is None:
            raise FileNotFoundError(
                _errno.ENOENT, "No such file or directory", str(src)
            )
        # The same file under another spelling (a case-only rename on a
        # case-insensitive filesystem, or `x.move(x)`) is renamed in place:
        # removing the "existing" target would delete the source itself.
        if not _same_file(src, target):
            target_stat = FileStat.from_path(target, follow_symlink=False)
            if target_stat is not None:
                if not overwrite:
                    raise FileExistsError(target)
                if target_stat.is_dir() and not target_stat.is_symlink():
                    if not src_stat.is_dir():
                        raise IsADirectoryError(target)
                    target.rm(recursive=True, missing_ok=True)
                elif (
                    native
                    and type(target) is type(src)
                    and callable(getattr(src, "replace", None))
                ):
                    # Local paths: os.replace() swaps atomically and leaves
                    # the target untouched if it fails (e.g. a locked source).
                    try:
                        return src.replace(target)
                    except OSError as error:
                        if error.errno != _errno.EXDEV:
                            raise
                        native = False
                else:
                    target.unlink(missing_ok=True)

        if native:
            try:
                return src.rename(target)
            except NotImplementedError:
                pass
            except OSError as error:
                # Another filesystem or drive: fall back to copy + delete, as
                # shutil.move and 3.14's Path.move do.
                if error.errno != _errno.EXDEV:
                    raise

        if src.is_dir():
            src.copy(target, overwrite=overwrite, recursive=True)
            src.rm(recursive=True)
        else:
            src.copy(target, overwrite=overwrite)
            src.unlink()


def _same_file(src: Path, target: Path) -> bool:
    """Whether `src` and `target` name the same file, conservatively.

    `samefile()` is trusted only between paths of the same concrete type:
    `LocalPath.samefile()` accepts any os.PathLike, so a remote target whose
    `__fspath__()` happens to spell a local path would otherwise "match".
    Where `samefile()` is unavailable (no st_dev/st_ino), equal paths on the
    same backend are the same file.
    """
    if type(src) is not type(target):
        return False
    try:
        return bool(src.samefile(target))
    except (NotImplementedError, OSError, TypeError, ValueError):
        pass
    if getattr(src, "_backend", None) is not getattr(target, "_backend", None):
        return False
    try:
        return src == target
    except Exception:
        return False


PathLike = _ty.Union[str, Path]
