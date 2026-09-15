from __future__ import annotations

import functools as _func
import ntpath as _ntpath
import os as _os
import pathlib as _path
import posixpath as _posixpath
import re as _re
import shutil as _shutil
import stat as _stat
import sys as _sys
import types as _types
import typing as _ty

from . import path as _proto
from . import utils as _utils
from .utils import glob as _glob
from .utils.stat import FileStat as _FileStat

# pathlib.Path.stat()/chmod() only accept follow_symlinks= on 3.10+; below
# that, LocalPath (which inherits them directly from pathlib.Path via MRO,
# see class LocalPath below) needs a shim.
_HAS_FOLLOW_SYMLINKS = _sys.version_info >= (3, 10)


@_func.cache
def _is_case_sensitive(flavour: _os.path) -> bool:
    return flavour.normcase("Aa") == "Aa"


def _translate_segment(part: str, not_sep: str) -> str:
    """One pattern segment to a regex in which `*`/`?` never match `sep`
    (CPython 3.13's `fnmatch._translate(part, not_sep + "*", not_sep)`)."""
    import fnmatch as _fnmatch

    # fnmatch.translate() wraps its output; slice that off a bracket
    # expression's translation to reuse its range/negation handling.
    prefix, suffix = _fnmatch.translate("_").split("_")
    out = []
    i, n = 0, len(part)
    while i < n:
        c = part[i]
        i += 1
        if c == "*":
            if not out or out[-1] != f"{not_sep}*":
                out.append(f"{not_sep}*")
        elif c == "?":
            out.append(not_sep)
        elif c == "[":
            j = i
            if j < n and part[j] == "!":
                j += 1
            if j < n and part[j] == "]":
                j += 1
            while j < n and part[j] != "]":
                j += 1
            if j >= n:
                out.append("\\[")
            else:
                out.append(
                    _fnmatch.translate(part[i - 1 : j + 1])[len(prefix) : -len(suffix)]
                )
                i = j + 1
        else:
            out.append(_re.escape(c))
    return "".join(out)


@_func.lru_cache(maxsize=512)
def _compile_full_match(pattern: str, sep: str, case_sensitive: bool):
    """Compile a `full_match()` pattern string the way CPython 3.13 does:
    `glob.translate(pattern, recursive=True, include_hidden=True, seps=sep)`."""
    esc = _re.escape(sep)
    not_sep = f"[^{esc}]"
    results = []
    parts = pattern.split(sep)
    last = len(parts) - 1
    for idx, part in enumerate(parts):
        if part == "*":
            results.append(f"{not_sep}+{esc}" if idx < last else f"{not_sep}+")
        elif part == "**":
            if idx < last:
                if parts[idx + 1] != "**":
                    results.append(f"(?:.+{esc})?")
            else:
                results.append(".*")
        else:
            if part:
                results.append(_translate_segment(part, not_sep))
            if idx < last:
                results.append(esc)
    flags = 0 if case_sensitive else _re.IGNORECASE
    return _re.compile(f"(?s:{''.join(results)})\\Z", flags).match


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

    # match()/full_match(): stdlib's own wherever it has the promised
    # signature. `PurePath` follows this class in the MRO, so defining either
    # name unconditionally would displace stdlib's on the versions that have
    # it -- hence the version-gated definitions.

    if _sys.version_info < (3, 12):

        def match(self, path_pattern, *, case_sensitive=None):
            """Return True if this path matches the given pattern (stdlib's
            `PurePath.match`). `case_sensitive=` is 3.12+ in CPython; this
            shim accepts it on older versions too."""
            if case_sensitive is None:
                return _path.PurePath.match(self, path_pattern)
            return self._match_case(_os.fspath(path_pattern), case_sensitive)

        def _match_case(self, pattern: str, case_sensitive: bool) -> bool:
            # CPython 3.9-3.11's PurePath.match, with the flavour's casefold
            # replaced by the requested sensitivity.
            import fnmatch as _fnmatch

            fold = (lambda s: s) if case_sensitive else str.lower
            pat = self.with_segments(fold(pattern))
            pat_parts = list(pat.parts)
            if not pat_parts:
                raise ValueError("empty pattern")
            if pat.drive and pat.drive != fold(self.drive):
                return False
            if pat.root and pat.root != self.root:
                return False
            parts = [fold(part) for part in self.parts]
            if pat.drive or pat.root:
                if len(pat_parts) != len(parts):
                    return False
                pat_parts = pat_parts[1:]
            elif len(pat_parts) > len(parts):
                return False
            return all(
                _fnmatch.fnmatchcase(part, pat)
                for part, pat in zip(reversed(parts), reversed(pat_parts))
            )

    if _sys.version_info < (3, 13):

        def full_match(self, pattern, *, case_sensitive=None):
            """Return True if this path matches the glob-style `pattern`
            against the whole path, with "**" matching any number of
            segments -- a port of CPython 3.13's `PurePath.full_match`. The
            generic `Pathname.full_match` splits on "/" and never sees a
            drive, a root or a backslash, so rooted and Windows patterns
            failed before 3.13."""
            if not isinstance(pattern, _path.PurePath):
                pattern = self.with_segments(pattern)
            if case_sensitive is None:
                case_sensitive = self._is_case_sensitive
            sep = "\\" if isinstance(pattern, _path.PureWindowsPath) else "/"
            path_str = str(self)
            pattern_str = str(pattern)
            match = _compile_full_match(
                "" if pattern_str == "." else pattern_str, sep, case_sensitive
            )
            return match("" if path_str == "." else path_str) is not None


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

    def _rename_compatible(self, target) -> bool:
        if isinstance(target, _path.PurePath):
            return True
        # FileUri: a local file under a URI spelling.
        try:
            return isinstance(getattr(target, "filepath", None), _path.PurePath)
        except Exception:
            return False

    def _is_junction_link(self) -> bool:
        # lstat() reports a junction (IO_REPARSE_TAG_MOUNT_POINT) as a plain
        # directory -- CPython only rewrites the mode to S_IFLNK for real
        # symlinks -- so rm(recursive=True) used to walk into it and delete
        # the junction target's files. shutil.rmtree guards the same case.
        if _os.name != "nt":
            return False
        try:
            st = _os.lstat(self)
        except OSError:
            return False
        return getattr(st, "st_reparse_tag", 0) == getattr(
            _stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003
        )

    def walk(self, top_down=True, on_error=None, follow_symlinks=False):
        # 3.12+ stdlib `pathlib.Path.walk()` sits ahead of ours in the MRO
        # and would otherwise win here. Its implementation calls
        # `self._scandir()` expecting stdlib's own context-manager-capable
        # `os.scandir(self)` return value ("with scandir_it:") -- our
        # `_scandir()` override above is a plain generator, so stdlib's
        # `walk()` breaks on it (`TypeError: 'generator' object does not
        # support the context manager protocol`). Route explicitly to our
        # own `walk()` (which drives `_scandir()` correctly) regardless of
        # which one the MRO would otherwise resolve to.
        return _proto.Path.walk(
            self, top_down=top_down, on_error=on_error, follow_symlinks=follow_symlinks
        )

    def copy(
        self,
        target,
        *,
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=True,
        recursive=False,
        ignore_error=None,
        progress=None,
    ):
        # Python 3.14 added pathlib.Path.copy(), which sits ahead of our
        # generic implementation in the MRO and does not accept pathlib_next's
        # overwrite=/recursive=/ignore_error=/progress= extensions. Keep
        # LocalPath's cross-version contract stable by routing explicitly to
        # our method.
        return _proto.Path.copy(
            self,
            target,
            overwrite=overwrite,
            follow_symlinks=follow_symlinks,
            preserve_metadata=preserve_metadata,
            recursive=recursive,
            ignore_error=ignore_error,
            progress=progress,
        )

    def move(self, target, *, overwrite=False):
        # Python 3.14 added pathlib.Path.move() alongside copy(); route around
        # the same MRO collision so overwrite= and the generic fallback remain
        # available on every supported Python version.
        return _proto.Path.move(self, target, overwrite=overwrite)

    def _symlink_to(
        self, target: _proto.Path | str, target_is_directory: bool = False
    ) -> None:
        # `symlink_to` is in _OPERATION_NAMES, so the generic
        # `Path.symlink_to()` (which owns `force=`) is what resolves on
        # LocalPath -- it delegates the actual link creation here, and
        # stdlib's own implementation is reached explicitly via super().
        # Unlike every remote scheme, target_is_directory is meaningful
        # here: it is the Windows-only flag pathlib forwards to
        # os.symlink(). stdlib accepts any os.PathLike, so the normalized
        # path object goes straight through -- and a relative target stays
        # relative, exactly as before.
        return super().symlink_to(target, target_is_directory)

    def stat(self, *, follow_symlinks=True):
        # pathlib.Path.stat() (next in MRO via WindowsPath/PosixPath) only
        # accepts follow_symlinks= on 3.10+; below that, lstat() is the
        # (pre-existing, non-kwarg) equivalent for follow_symlinks=False.
        if _HAS_FOLLOW_SYMLINKS:
            return super().stat(follow_symlinks=follow_symlinks)
        return super().stat() if follow_symlinks else super().lstat()

    def chmod(self, mode: int | str, *, follow_symlinks: bool = True):
        # Same follow_symlinks= 3.10+ gap as stat() above; lchmod() is the
        # pre-existing equivalent (raises NotImplementedError itself on
        # platforms without os.lchmod, e.g. Windows).
        mode = _utils.as_mode(mode)
        if _HAS_FOLLOW_SYMLINKS:
            return super().chmod(mode, follow_symlinks=follow_symlinks)
        return super().chmod(mode) if follow_symlinks else super().lchmod(mode)

    def _chown(
        self,
        uid: int | str | None,
        gid: int | str | None,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        # shutil.chown() is the stdlib spelling that accepts names as well
        # as ids, and it takes None for "leave unchanged" -- the same
        # canonical form Chmod.chown() already normalized to, so the pair
        # passes straight through. os.chown's -1 sentinel never appears
        # here.
        if not hasattr(_os, "chown"):
            # shutil.chown() *exists* on Windows while os.chown does not, so
            # without this the int form leaked `AttributeError: module 'os'
            # has no attribute 'chown'` and the name form leaked
            # `LookupError: no such user` -- actively misleading, since
            # there is no `pwd` module for shutil._get_uid to consult, so
            # every name "misses" whether or not the user exists.
            # docs/divergences.md promises NotImplementedError here.
            raise NotImplementedError("chown()")
        if not follow_symlinks:
            if not hasattr(_os, "lchown"):
                raise NotImplementedError("chown(follow_symlinks=False)")
            if isinstance(uid, str) or isinstance(gid, str):
                # os.lchown takes numeric ids only; resolving a name would
                # mean duplicating shutil's lookup, and guessing wrong here
                # writes the wrong owner silently.
                raise NotImplementedError(
                    "chown(follow_symlinks=False) requires numeric uid/gid"
                )
            return _os.lchown(
                self, -1 if uid is None else uid, -1 if gid is None else gid
            )
        return _shutil.chown(self, uid, gid)

    def glob(
        self,
        pattern: str | _proto.FsPathLike,
        *,
        case_sensitive: bool = None,
        include_hidden: bool = True,
        recursive: bool = None,
        dironly: bool = None,
        recurse_symlinks: bool = False,
    ):
        """Iterate over this subtree and yield all existing files (of any
        kind, including directories) matching the given relative pattern.

        Same semantics as Path.glob(); every separator of this flavour
        splits the pattern, and a pattern with a drive or root raises
        `glob.NonRelativePatternError` like pathlib.
        """
        pattern = _os.fspath(pattern)
        if pattern:
            anchored = self.with_segments(pattern)
            if anchored.drive or anchored.root:
                raise _glob.NonRelativePatternError(
                    "Non-relative patterns are unsupported"
                )
            for sep in self._path_separators:
                pattern = pattern.replace(sep, "/")
            if any(self._parser.splitdrive(part)[0] for part in pattern.split("/")):
                # "sub/C:/x": joining "C:" would re-anchor outside self, and
                # no child can be named that, so nothing matches (pathlib).
                return iter(())
        return _proto.Path.glob(
            self,
            pattern,
            case_sensitive=case_sensitive,
            include_hidden=include_hidden,
            recursive=recursive,
            dironly=dironly,
            recurse_symlinks=recurse_symlinks,
        )
