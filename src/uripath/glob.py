# Based on glob built in on python modified to work with Uri/anything that implemetns fspath/iterdir


"""Filename globbing utility."""

import contextlib
import os
import re
import fnmatch
import itertools
import stat
import posixpath
import typing as _ty

RECURSIVE = "**"
WILCARD_PATTERN = re.compile("([*?[])")
HIDDEN_PREFIX = "."


class _Globable:
    def __iter__(self) -> _ty.Iterator[_ty.Self]: ...
    @property
    def name(self) -> str: ...

    def __fspath__(self) -> str: ...

    def __truediv__(self, key: _ty.Self | str) -> _ty.Self: ...

    @property
    def parent(self) -> _ty.Self: ...

    def name(self) -> str: ...

    def is_dir(self) -> bool: ...
    def exists(self) -> bool: ...


def has_glob_wildard(s: str):
    return WILCARD_PATTERN.search(s) is not None


def _ishidden(path: str):
    return path.startswith(HIDDEN_PREFIX)


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
    include_hidden=False
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
        )
    )


def iglob(
    path: _Globable,
    *,
    root_dir: _Globable = None,
    recursive=False,
    include_hidden=False
):
    """Return an iterator which yields the paths matching a pathname pattern.

    The pattern may contain simple shell-style wildcards a la
    fnmatch. However, unlike fnmatch, filenames starting with a
    dot are special cases that are not matched by '*' and '?'
    patterns.

    If recursive is true, the pattern '**' will match any files and
    zero or more directories and subdirectories.
    """

    it = _iglob(path, root_dir, recursive, False, include_hidden=include_hidden)
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
):
    pathname = os.fspath(path)
    _dironly = pathname.endswith("/")
    parent = path.parent
    parent = path.parent if parent != path else None
    if not has_glob_wildard(pathname):
        assert not dironly
        if not _dironly:
            if path.exists():
                yield path
        else:
            # Patterns ending with a slash should match only directories
            if parent and parent.is_dir():
                yield path
        return

    if not parent:
        if recursive and path.name == RECURSIVE:
            yield from _glob_recursive(
                root_dir, path.name, dironly, include_hidden=include_hidden
            )
        else:
            yield from _glob_with_pattern(
                root_dir, path.name, dironly, include_hidden=include_hidden
            )
        return

    if parent and has_glob_wildard(parent.name):
        dirs = _iglob(parent, root_dir, recursive, True, include_hidden=include_hidden)
    else:
        dirs = [parent]
    if has_glob_wildard(path.name):
        if recursive and path.name == RECURSIVE:
            glob_in_dir = _glob_recursive
        else:
            glob_in_dir = _glob_with_pattern
    else:
        glob_in_dir = _glob_exact_match
    for parent in dirs:
        for name in glob_in_dir(
            _join(root_dir, parent),
            path.name,
            dironly,
            include_hidden=include_hidden,
        ):
            yield parent / name


# These 2 helper functions non-recursively glob inside a literal directory.
# They return a list of basenames.  _glob1 accepts a pattern while _glob0
# takes a literal basename (so it only has to check for its existence).


def _glob_with_pattern(
    parent: _Globable, pattern: str, dironly: bool, include_hidden=False
):
    if not include_hidden or not _ishidden(pattern):

        def _filter(p: str):
            return not _ishidden(p)

    else:

        def _filter(p: str):
            return True

    for name in _iterdir(parent, dironly):
        if _filter(name) and fnmatch.fnmatchcase(name, pattern):
            yield name


def _glob_exact_match(
    parent: _Globable, basename: str, dironly: bool, include_hidden=False
):
    if basename:
        if _join(parent, basename).exists():
            return [basename]
    else:
        if parent.is_dir():
            return [basename]
    return []


# This helper function recursively yields relative pathnames inside a literal
# directory.


def _glob_recursive(
    parent: _Globable, pattern: str, dironly: bool, include_hidden=False
):
    assert pattern == RECURSIVE
    if not parent or parent.is_dir():
        yield ""
    yield from _rlistdir(parent, dironly, include_hidden=include_hidden)


# If dironly is false, yields all file names inside a directory.
# If dironly is true, yields only directory names.
def _iterdir(path: _Globable, dironly: bool):
    for entry in path:
        try:
            if not dironly or entry.is_dir():
                yield entry.name
        except OSError:
            pass


# Recursively yields relative pathnames inside a literal directory.
def _rlistdir(dirname: _Globable, dironly: bool, include_hidden=False):
    names = _iterdir(dirname, dironly)
    for x in names:
        if include_hidden or not _ishidden(x):
            yield x
            path = _join(dirname, x) if dirname else x
            for y in _rlistdir(path, dironly, include_hidden=include_hidden):
                yield _join(x, y)
