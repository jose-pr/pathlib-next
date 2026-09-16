"""Filename globbing over any pathlib_next `Path`.

Modelled on CPython's pathlib glob selectors, reworked to run over anything
that implements `_scandir()`/`iterdir()`/`is_dir()`/`name` like
`pathlib.Path` (`LocalPath`, `MemPath`, every `UriPath` scheme).
"""

from __future__ import annotations

import fnmatch as _fnmatch
import functools as _func
import re as _re
import sys as _sys
import typing as _ty

RECURSIVE = "**"
ANY_PATTERN = _re.compile(_fnmatch.translate("*"))
WILDCARD_PATTERN = _re.compile("([*?[])")
WILCARD_PATTERN = WILDCARD_PATTERN  # back-compat alias for the old typo'd name

# CPython 3.13 made a trailing "**" select files as well as directories;
# earlier versions select directories only. glob() follows the running
# interpreter so LocalPath keeps matching the pathlib it runs next to.
_DOUBLESTAR_SELECTS_FILES = _sys.version_info >= (3, 13)

# The two rules the running interpreter changed mid-series, honoured when
# `native=True` (the default) and smoothed over when it is False:
#   - a trailing "/" selects directories only from 3.11; before that pathlib
#     ignores it entirely.
#   - "**" is only a recursive component when it is the WHOLE component;
#     "a**" raises before 3.13 and is a plain wildcard from 3.13.
_TRAILING_SLASH_SELECTS_DIRS = _sys.version_info >= (3, 11)
_PARTIAL_DOUBLESTAR_ALLOWED = _sys.version_info >= (3, 13)

if _ty.TYPE_CHECKING:
    from ..path import P as _Globable
else:

    class _Globable(_ty.Protocol): ...


class NonRelativePatternError(NotImplementedError, ValueError):
    """An absolute or anchored glob pattern. pathlib raises
    `NotImplementedError("Non-relative patterns are unsupported")`; this is
    also a `ValueError`, so either `except` clause catches it."""


@_func.lru_cache(maxsize=256, typed=True)
def compile_pattern(pat: str, case_sensitive: bool):
    # re.NOFLAG is 3.11+
    flags = 0 if case_sensitive else _re.IGNORECASE
    return _re.compile(_fnmatch.translate(pat), flags)


def _collapse_recursive(parts: _ty.Iterable[str]) -> _ty.List[str]:
    """Drop repeated consecutive "**" (they select the same paths)."""
    collapsed: _ty.List[str] = []
    for part in parts:
        if part == RECURSIVE and collapsed and collapsed[-1] == RECURSIVE:
            continue
        collapsed.append(part)
    return collapsed


def full_match(segments: _ty.Sequence[str], pattern: str, case_sensitive: bool) -> bool:
    """Match `segments` against a glob pattern that may contain "**"
    components (pathlib 3.13's PurePath.full_match semantics): a "**" matches
    zero or more segments, except a trailing "**" after other components,
    which needs at least one ("a/**" does not match "a").

    Runs as a set-of-states automaton, O(len(segments) * len(pattern)), so
    repeated "**" cannot backtrack exponentially.
    """
    pats = pattern.split("/")
    pats = _collapse_recursive(p for i, p in enumerate(pats) if p or i == 0)
    segs = [s for i, s in enumerate(segments) if s or i == 0]
    end = len(pats)

    def closure(states: _ty.Set[int]) -> _ty.Set[int]:
        for i in sorted(states):
            # A non-trailing (or sole) "**" may match zero segments.
            if i < end and pats[i] == RECURSIVE and (i < end - 1 or end == 1):
                states.add(i + 1)
        return states

    states = closure({0})
    for seg in segs:
        advanced: _ty.Set[int] = set()
        for i in states:
            if i == end:
                continue
            pat = pats[i]
            if pat == RECURSIVE:
                advanced.update((i, i + 1))
            elif compile_pattern(pat, case_sensitive).match(seg):
                advanced.add(i + 1)
        if not advanced:
            return False
        states = closure(advanced)
    return end in states


def parse_pattern(
    pattern: "str | _ty.Any", *, native: bool = True
) -> _ty.Tuple[_ty.List[str], bool]:
    """Split a relative glob `pattern` (a `/`-separated string or a
    `Pathname`) into its components, and report whether it ended with a
    separator (directories only).

    Raises `ValueError` for an empty pattern and `NonRelativePatternError`
    for an absolute one, as `pathlib.Path.glob()` does. Empty and "."
    components are dropped.

    `native=True` (the default) follows the running interpreter on the two
    rules pathlib changed mid-series: a trailing "/" is ignored before 3.11
    (it selects directories only from 3.11), and a component that merely
    CONTAINS "**" ("a**") raises `ValueError` before 3.13. `native=False`
    applies one rule on every version instead -- a trailing "/" always
    selects directories only, "a**" is always a plain wildcard -- so a
    pattern gives the same answer on every interpreter and every backend.
    """
    if isinstance(pattern, str):
        rooted = pattern.startswith("/")
        segments = pattern.split("/")
    else:
        segments = list(pattern.segments)
        rooted = bool(getattr(pattern, "anchor", "")) or bool(
            getattr(pattern, "source", None)
        )
    if rooted:
        raise NonRelativePatternError("Non-relative patterns are unsupported")
    parts = [part for part in segments if part not in ("", ".")]
    if not parts:
        raise ValueError(f"Unacceptable pattern: {str(pattern)!r}")
    if native and not _PARTIAL_DOUBLESTAR_ALLOWED:
        for part in parts:
            if RECURSIVE in part and part != RECURSIVE:
                # pathlib's own message, so an `except ValueError` that reads
                # it sees the same text it does on this interpreter.
                raise ValueError(
                    "Invalid pattern: '**' can only be an entire path component"
                )
    trailing_sep = bool(segments) and segments[-1] == ""
    if native and not _TRAILING_SLASH_SELECTS_DIRS:
        trailing_sep = False
    return parts, trailing_sep


def glob(
    path: _Globable,
    *,
    dironly: bool = False,
    root_dir: _Globable | None = None,
    recursive: bool = False,
    include_hidden: bool = False,
    case_sensitive: bool | None = None,
) -> _ty.Iterable[_Globable]:
    """Return an iterator which yields the paths matching a pathname pattern.

    `path` is the pattern itself, as a path (e.g. `UriPath("file:/x/**/*.py")`).
    The pattern may contain simple shell-style wildcards a la fnmatch. Like
    the stdlib `glob` module, and unlike `Path.glob()`, hidden entries (names
    starting with a dot) are not matched by wildcards and not descended into
    by "**" unless `include_hidden` is true.

    If recursive is true, the pattern '**' will match any files and
    zero or more directories and subdirectories.
    """
    segments = list(path.segments)
    if len(segments) > 1 and segments[-1] == "":
        segments.pop()
        dironly = True
    first_wildcard = next(
        (i for i, seg in enumerate(segments) if WILDCARD_PATTERN.search(seg)),
        max(len(segments) - 1, 0),
    )
    base = path.with_segments(*segments[:first_wildcard])
    if root_dir is not None:
        base = root_dir / base
    parts = [part for part in segments[first_wildcard:] if part not in ("", ".")]
    if not parts:
        if base.is_dir() if dironly else base.exists():
            yield base
        return
    yield from select(
        base,
        parts,
        dironly=dironly,
        recursive=recursive,
        include_hidden=include_hidden,
        case_sensitive=case_sensitive,
    )


def select(
    base: _Globable,
    parts: _ty.Sequence[str],
    *,
    dironly: bool = False,
    recursive: bool = True,
    include_hidden: bool = True,
    case_sensitive: bool | None = None,
) -> _ty.Iterator[_Globable]:
    """Yield the paths under `base` matching the pattern components `parts`
    (see `parse_pattern()`); the engine behind `Path.glob()`.

    Directory listings go through `_scandir()`; an `OSError` from listing a
    missing or non-directory path selects nothing. "**" (when `recursive`)
    decides recursion from the listing's non-following stat, so it never
    descends into a directory symlink and always terminates. A trailing "**"
    also selects files on Python 3.13+, directories only before.
    """
    default_case = getattr(base, "_is_case_sensitive", True)
    if case_sensitive is None:
        case_sensitive = default_case
    if recursive:
        parts = _collapse_recursive(parts)
    steps: _ty.List[_ty.Tuple[str, _ty.Any]] = []
    for part in parts:
        if recursive and part == RECURSIVE:
            steps.append((part, None))
        elif WILDCARD_PATTERN.search(part) or case_sensitive != default_case:
            steps.append((part, compile_pattern(part, case_sensitive)))
        else:
            steps.append((part, False))
    opts = _Options(dironly, include_hidden)
    selected = _select(base, steps, 0, opts, None)
    if sum(1 for _, kind in steps if kind is None) < 2:
        yield from selected
        return
    # Two separate "**" can reach one path along several splits.
    seen = set()
    for path in selected:
        if path not in seen:
            seen.add(path)
            yield path


class _Options(_ty.NamedTuple):
    dironly: bool
    include_hidden: bool


def _select(
    path: _Globable,
    steps: _ty.Sequence[_ty.Tuple[str, _ty.Any]],
    index: int,
    opts: _Options,
    is_dir: bool | None,
) -> _ty.Iterator[_Globable]:
    part, kind = steps[index]
    last = index == len(steps) - 1

    if kind is None:  # "**"
        if is_dir is None and not path.is_dir():
            return
        if last:
            with_files = _DOUBLESTAR_SELECTS_FILES and not opts.dironly
            yield from _recurse(path, opts.include_hidden, with_files)
            return
        for directory in _recurse(path, opts.include_hidden, False):
            yield from _select(directory, steps, index + 1, opts, True)
        return

    if kind is False:  # literal component: no listing needed
        child = _child(path, part, None)
        if not last:
            yield from _select(child, steps, index + 1, opts, None)
        elif child.is_dir() if opts.dironly else child.exists():
            yield child
        return

    need_dir = opts.dironly or not last
    skip_hidden = not opts.include_hidden and not part.startswith(".")
    for child, stat in _scan(path):
        if skip_hidden and child.is_hidden():
            continue
        if not kind.match(child.name):
            continue
        if need_dir and not _entry_is_dir(child, stat, follow_symlinks=True):
            continue
        if last:
            yield child
        else:
            yield from _select(child, steps, index + 1, opts, True)


def _recurse(
    top: _Globable, include_hidden: bool, with_files: bool
) -> _ty.Iterator[_Globable]:
    """Yield `top` and every directory below it (plus every other entry when
    `with_files`), never descending through a directory symlink."""
    yield top
    stack = [top]
    while stack:
        directory = stack.pop()
        for child, stat in _scan(directory):
            if not include_hidden and child.is_hidden():
                continue
            is_dir = _entry_is_dir(child, stat, follow_symlinks=False)
            if is_dir or with_files:
                yield child
            if is_dir:
                stack.append(child)


def _scan(directory: _Globable):
    """(child, non-following stat or None) per entry; nothing if listing
    fails. Materialized so a lazily-raising listing is still caught here."""
    scandir = getattr(directory, "_scandir", None)
    try:
        if scandir is None:
            return [(child, None) for child in directory.iterdir()]
        return [(_child(directory, name, stat), stat) for name, stat in scandir()]
    except OSError:
        return ()


def _child(directory: _Globable, name: str, stat) -> _Globable:
    if getattr(directory, "_pop_stat_hint", None) is not None:
        # UriPath: `/` would re-parse the name as URI syntax ("a?b" -> query),
        # and seeding the listing's stat saves a round trip per entry.
        return directory._make_child_relpath(name, stat_hint=stat)
    return directory / name


def _entry_is_dir(path: _Globable, stat, *, follow_symlinks: bool) -> bool:
    if stat is not None and not (follow_symlinks and stat.is_symlink()):
        return stat.is_dir()
    if follow_symlinks:
        return path.is_dir()
    return path.is_dir() and not path.is_symlink()
