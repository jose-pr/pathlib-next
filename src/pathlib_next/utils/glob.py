# Based on glob built-in on python modified to work with Uri/anything that implemetns fspath/iterdir that is similar to pathlib.Path


"""Filename globbing utility."""

import fnmatch as _fnmatch
import typing as _ty
import functools as _func
import re as _re


RECURSIVE = "**"
ANY_PATTERN = _re.compile(_fnmatch.translate("*"))
WILCARD_PATTERN = _re.compile("([*?[])")

if _ty.TYPE_CHECKING:
    from ..path import P as _Globable
else:

    class _Globable(_ty.Protocol): ...


@_func.lru_cache(maxsize=256, typed=True)
def compile_pattern(pat: str, case_sensitive: bool):
    flags = _re.NOFLAG if case_sensitive else _re.IGNORECASE
    return _re.compile(_fnmatch.translate(pat), flags)


def glob(
    path: _Globable,
    *,
    dironly: bool = False,
    root_dir: _Globable | None = None,
    recursive: bool = False,
    include_hidden: bool = False,
    case_sensitive: bool | None = None
) -> _ty.Iterable[_Globable]:
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

    include_hidden = include_hidden or path.is_hidden()
    pattern = compile_pattern(path.name, case_sensitive) if path.name else ANY_PATTERN

    name_is_pattern = WILCARD_PATTERN.match(path.name) != None
    wildcard_in_path = name_is_pattern
    parent = next(iter(path.parents), None)
    for segment in path.parts:
        if WILCARD_PATTERN.match(segment) != None:
            wildcard_in_path = True
            break

    root: _Globable = (
        (root_dir or parent) if not root_dir or not parent else (root_dir / parent)
    )

    if recursive and path.name == RECURSIVE:
        globber = _glob_recursive
    else:
        globber = _glob_with_pattern

    if not parent or not wildcard_in_path:
        yield from globber(
            root or path,
            pattern,
            dironly,
            include_hidden=include_hidden,
        )
        return

    if parent and name_is_pattern:
        dirs = glob(
            parent,
            root_dir=root_dir,
            recursive=recursive,
            dironly=True,
            include_hidden=include_hidden,
            case_sensitive=case_sensitive,
        )
    else:
        dirs = [parent]

    for parent in dirs:
        yield from globber(parent, pattern, dironly, include_hidden)


def _glob_with_pattern(
    parent: _Globable, pattern: _re.Pattern, dironly: bool, include_hidden=False
) -> _ty.Iterable[_Globable]:
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
    for entry in path.iterdir():
        try:
            if not dironly or entry.is_dir():
                yield entry
        except OSError:
            pass


# Recursively yields relative pathnames inside a literal directory.
def _rlistdir(
    dirname: _Globable, dironly: bool, include_hidden=False
) -> _ty.Iterable[_Globable]:
    for path in _iterdir(dirname, dironly):
        if include_hidden or not path.is_hidden():
            yield path
            for y in _rlistdir(path, dironly, include_hidden):
                yield y
