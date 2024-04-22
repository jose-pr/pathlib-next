# Based on glob built-in on python modified to work with Uri/anything that implemetns fspath/iterdir that is similar to pathlib.Path


"""Filename globbing utility."""

import os
import re
import fnmatch
import itertools
import typing as _ty
import functools as _func
import re as _re


RECURSIVE = "**"
WILCARD_PATTERN = re.compile("([*?[])")

if _ty.TYPE_CHECKING:
    from .protocols import PathProtocol as _Globable
else:

    class _Globable(_ty.Protocol): ...


@_func.lru_cache(maxsize=256, typed=True)
def compile_pattern(pat: str, case_sensitive: bool):
    flags = _re.NOFLAG if case_sensitive else _re.IGNORECASE
    return _re.compile(fnmatch.translate(pat), flags)


def has_glob_wildard(s: str):
    return WILCARD_PATTERN.search(s) is not None


def _join(dir: _Globable, path: _Globable):
    # It is common if dirname or basename is empty
    if not dir or not path:
        return dir or path
    return dir / path


def glob(
    pathname: _Globable,
    *,
    root_dir: _Globable = None,
    recursive=False,
    include_hidden=False,
    case_sensitive: bool = None
):
    """Return a list of paths matching a pathname pattern.

    The pattern may contain simple shell-style wildcards a la
    fnmatch. Unlike fnmatch, filenames starting with a
    dot are special cases that are not matched by '*' and '?'
    patterns by default.

    If `include_hidden` is true, the patterns '*', '?', '**'  will match hidden
    directories.

    If `recursive` is true, the pattern '**' will match any files and
    zero or more directories and subdirectories.
    """
    return list(
        iglob(
            pathname,
            root_dir=root_dir,
            recursive=recursive,
            include_hidden=include_hidden,
            case_sensitive=case_sensitive,
        )
    )


def iglob(
    path: _Globable,
    *,
    root_dir: _Globable = None,
    recursive=False,
    include_hidden=False,
    case_sensitive: bool = None
):
    """Return an iterator which yields the paths matching a pathname pattern.

    The pattern may contain simple shell-style wildcards a la
    fnmatch. However, unlike fnmatch, filenames starting with a
    dot are special cases that are not matched by '*' and '?'
    patterns.

    If recursive is true, the pattern '**' will match any files and
    zero or more directories and subdirectories.
    """
    if case_sensitive is None:
        case_sensitive = path._is_case_sensitive
    it = _iglob(
        path,
        root_dir,
        recursive,
        False,
        include_hidden=include_hidden,
        case_sensitive=case_sensitive,
    )
    path_ = os.fspath(path)
    if not path_ or recursive and path_.startswith(RECURSIVE):
        try:
            s = next(it)  # skip empty string
            if s:
                it = itertools.chain((s,), it)
        except StopIteration:
            pass
    return it


def _iglob(
    path: _Globable,
    root_dir: _Globable | None,
    recursive: bool,
    dironly: bool,
    include_hidden=False,
    case_sensitive: bool = None,
):
    pathname = os.fspath(path)
    _dironly = pathname.endswith("/")
    parent = path.parent
    parent = parent if parent != path and parent else None
    include_hidden = path.name.startswith(".")
    pattern = compile_pattern(path.name, case_sensitive)
    root = _join(root_dir, parent)
    if not has_glob_wildard(pathname):
        assert not dironly
        if not _dironly:
            yield from _glob_with_pattern(
                root, pattern, False, include_hidden=include_hidden
            )
        else:
            # Patterns ending with a slash should match only directories
            if parent:
                yield from _glob_with_pattern(
                    root.parent, pattern, False, include_hidden=include_hidden
                )
        return

    if not parent:
        if recursive and path.name == RECURSIVE:
            yield from _glob_recursive(
                root,
                pattern,
                dironly,
                include_hidden=include_hidden,
                case_sensitive=case_sensitive,
            )
        else:
            yield from _glob_with_pattern(
                root, pattern, dironly, include_hidden=include_hidden
            )
        return

    if parent and has_glob_wildard(parent.name):
        dirs = _iglob(
            parent,
            root_dir,
            recursive,
            True,
            include_hidden=include_hidden,
            case_sensitive=case_sensitive,
        )
    else:
        dirs = [parent]

    if recursive and path.name == RECURSIVE:
        glob_in_dir = _glob_recursive
    else:
        glob_in_dir = _glob_with_pattern

    for parent in dirs:
        for _path in glob_in_dir(
            parent,
            pattern,
            dironly,
            include_hidden
        ):
            yield _path


# These 2 helper functions non-recursively glob inside a literal directory.
# They return a list of basenames.  _glob1 accepts a pattern while _glob0
# takes a literal basename (so it only has to check for its existence).


def _glob_with_pattern(
    parent: _Globable, pattern: re.Pattern, dironly: bool, include_hidden=False
):
    if not include_hidden:

        def _filter(p: _Globable):
            return not p.is_hidden()

    else:

        def _filter(p: _Globable):
            return True

    for path in _iterdir(parent, dironly):
        if _filter(path) and pattern.match(path.name):
            yield path


# This helper function recursively yields relative pathnames inside a literal
# directory.


def _glob_recursive(
    parent: _Globable, pattern: _re.Pattern, dironly: bool, include_hidden=False
):
    if parent and parent.is_dir():
        yield parent
    yield from _rlistdir(parent, dironly, include_hidden=include_hidden)


# If dironly is false, yields all file names inside a directory.
# If dironly is true, yields only directory names.
def _iterdir(path: _Globable, dironly: bool):
    for entry in path:
        try:
            if not dironly or entry.is_dir():
                yield entry
        except OSError:
            pass


# Recursively yields relative pathnames inside a literal directory.
def _rlistdir(dirname: _Globable, dironly: bool, include_hidden=False):
    for path in _iterdir(dirname, dironly):
        if include_hidden or not path.is_hidden():
            yield path
            for y in _rlistdir(path, dironly, include_hidden=include_hidden):
                yield y
