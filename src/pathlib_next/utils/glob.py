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

__all__ = [
    "NonRelativePatternError",
    "RECURSIVE",
    "full_match",
    "glob",
    "parse_pattern",
    "select",
]

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

# And two rules of how a literal component is checked, which `native=False`
# pins to the current pathlib's:
#   - before 3.13 a literal that is not the last component must be a
#     directory; from 3.13 it is joined unchecked and only the whole path is
#     tested.
#   - from 3.12 the last literal is tested without following a link, so a
#     dangling symlink is selected; before that it is tested with `exists()`.
_INTERMEDIATE_LITERAL_IS_CHECKED = _sys.version_info < (3, 13)
_FINAL_LITERAL_IS_NOT_FOLLOWED = _sys.version_info >= (3, 12)

#: Components that name a directory relative to the one before them.
_SPECIAL_PARTS = (".", "..")

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


def _fields(segments: _ty.Sequence[str]) -> _ty.List[str]:
    """`segments` as the fields of the path's "/"-joined text: the root is a
    leading empty field (`["", ""]` for the root alone), the empty path has
    none. Empty and "." segments are not names."""
    rooted = bool(segments) and segments[0] == ""
    names = [s for s in (segments[1:] if rooted else segments) if s and s != "."]
    if rooted:
        return ["", *names] if names else ["", ""]
    return names


def full_match(segments: _ty.Sequence[str], pattern: str, case_sensitive: bool) -> bool:
    """Match `segments` against a glob pattern that may contain "**"
    components (pathlib 3.13's PurePath.full_match semantics): a "**" matches
    zero or more segments, except a trailing "**" after other components,
    which needs at least one ("a/**" does not match "a").

    The root is a field of its own, as in the path's text: a rooted pattern
    names it with its leading "/", a lone "*" never matches it (a lone "*"
    matches one or more characters), and a "**" followed by more components
    reaches it only together with a name after it ("**/x" matches "/a/x",
    not "/x"). Empty and "." components are dropped from the pattern. A
    bracket expression never matches a separator (pathlib's regex lets a
    negated one).

    Runs as a set-of-states automaton, O(len(segments) * len(pattern)), so
    repeated "**" cannot backtrack exponentially.
    """
    fields = _fields(segments)
    parts = [part for part in pattern.split("/") if part and part != "."]
    if pattern.startswith("/"):
        parts = ["", *parts] if parts else ["", ""]
    pats = _collapse_recursive(parts)
    end = len(pats)
    last = end - 1

    # A state is (index, stage). Stage 0: about to match pats[index]. Stages
    # 1 and 2 belong to a "**": 1 once it has taken a single empty field (the
    # root), which does not yet count, 2 once it has taken enough and may
    # take more or hand over to the next component.
    def closure(states: _ty.Set[_ty.Tuple[int, int]]) -> _ty.Set[_ty.Tuple[int, int]]:
        pending = list(states)
        while pending:
            i, stage = pending.pop()
            if stage == 2 or (
                stage == 0
                and i < end
                and pats[i] == RECURSIVE
                # A non-trailing (or sole) "**" may match no field at all.
                and (i < last or end == 1)
            ):
                following = (i + 1, 0)
                if following not in states:
                    states.add(following)
                    pending.append(following)
        return states

    states = closure({(0, 0)})
    for field in fields:
        advanced: _ty.Set[_ty.Tuple[int, int]] = set()
        for i, stage in states:
            if stage:
                advanced.add((i, 2))
            elif i == end:
                continue
            elif pats[i] == RECURSIVE:
                advanced.add((i, 1 if field == "" and i < last else 2))
            elif pats[i] == "*":
                if field:
                    advanced.add((i + 1, 0))
            elif compile_pattern(pats[i], case_sensitive).match(field):
                advanced.add((i + 1, 0))
        if not advanced:
            return False
        states = closure(advanced)
    return (end, 0) in states


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
    recursive: bool | None = False,
    include_hidden: bool = False,
    case_sensitive: bool | None = None,
    native: bool = True,
    on_error: "_ty.Callable[[OSError], None] | None" = None,
    bound_loops: bool = False,
) -> _ty.Iterator[_Globable]:
    """Return an iterator which yields the paths matching a pathname pattern.

    `path` is the pattern itself, as a path (e.g. `UriPath("file:/x/**/*.py")`).
    The pattern may contain simple shell-style wildcards a la fnmatch. Like
    the stdlib `glob` module, and unlike `Path.glob()`, hidden entries (names
    starting with a dot) are not matched by wildcards and not descended into
    by "**" unless `include_hidden` is true.

    If recursive is true, the pattern '**' will match any files and
    zero or more directories and subdirectories. `recursive=None` decides
    from the pattern itself, as `Path.glob()` does: a "**" component enables
    it. `native=` follows the running interpreter's rules, or applies one
    rule on every version -- see `parse_pattern()`.

    The pattern is validated when this is called and the paths are selected
    lazily. The path's anchor (a drive, an extended-length drive prefix) is
    never a wildcard, whatever characters it holds.
    """
    segments = list(path.segments)
    if len(segments) > 1 and segments[-1] == "":
        segments.pop()
        if native and not _TRAILING_SLASH_SELECTS_DIRS:
            pass  # pathlib ignores a trailing separator before 3.11
        else:
            dironly = True
    if recursive is None:
        recursive = RECURSIVE in segments
    if native and not _PARTIAL_DOUBLESTAR_ALLOWED:
        for segment in segments:
            if RECURSIVE in segment and segment != RECURSIVE:
                raise ValueError(
                    "Invalid pattern: '**' can only be an entire path component"
                )
    anchor = getattr(path, "anchor", "")
    searched = 1 if segments and anchor and segments[0] == anchor else 0
    first_wildcard = next(
        (
            i
            for i, seg in enumerate(segments)
            if i >= searched and WILDCARD_PATTERN.search(seg)
        ),
        max(len(segments) - 1, 0),
    )
    base = path.with_segments(*segments[:first_wildcard])
    if root_dir is not None:
        base = root_dir / base
    parts = [part for part in segments[first_wildcard:] if part not in ("", ".")]
    return _expand(
        base,
        parts,
        dironly=dironly,
        recursive=recursive,
        include_hidden=include_hidden,
        case_sensitive=case_sensitive,
        native=native,
        on_error=on_error,
        bound_loops=bound_loops,
    )


def _expand(base, parts, *, dironly, **options) -> _ty.Iterator[_Globable]:
    if not parts:
        if base.is_dir() if dironly else base.exists():
            yield base
        return
    yield from select(base, parts, dironly=dironly, **options)


def select(
    base: _Globable,
    parts: _ty.Sequence[str],
    *,
    dironly: bool = False,
    recursive: bool = True,
    include_hidden: bool = True,
    case_sensitive: bool | None = None,
    native: bool = True,
    on_error: "_ty.Callable[[OSError], None] | None" = None,
    bound_loops: bool = False,
) -> _ty.Iterator[_Globable]:
    """Yield the paths under `base` matching the pattern components `parts`
    (see `parse_pattern()`); the engine behind `Path.glob()`.

    Directory listings go through `_scandir()`; an `OSError` from listing a
    missing or non-directory path selects nothing. "**" (when `recursive`)
    decides recursion from the listing's non-following stat, so it never
    descends into a directory symlink and always terminates. A trailing "**"
    also selects files on Python 3.13+ and directories only before, which is
    the third rule `native=False` pins: it then selects files on every
    version (3.13's rule, the one current pathlib applies).
    """
    # An explicit `case_sensitive` asks for names to be compared, so a literal
    # is looked for in the listing and yields the name as stored, as pathlib
    # does when it is given. "." and ".." are never names to find.
    explicit_case = case_sensitive is not None
    if case_sensitive is None:
        case_sensitive = getattr(base, "_is_case_sensitive", True)
    if recursive:
        parts = _collapse_recursive(parts)
    steps: _ty.List[_ty.Tuple[str, _ty.Any]] = []
    for part in parts:
        if recursive and part == RECURSIVE:
            steps.append((part, None))
        elif part in _SPECIAL_PARTS:
            steps.append((part, False))
        elif explicit_case or WILDCARD_PATTERN.search(part):
            steps.append((part, compile_pattern(part, case_sensitive)))
        else:
            steps.append((part, False))
    opts = _Options(
        dironly,
        include_hidden,
        _DOUBLESTAR_SELECTS_FILES if native else True,
        on_error,
        bound_loops,
        _INTERMEDIATE_LITERAL_IS_CHECKED and native,
        _FINAL_LITERAL_IS_NOT_FOLLOWED or not native,
    )
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
    doublestar_selects_files: bool = _DOUBLESTAR_SELECTS_FILES
    on_error: "_ty.Callable[[OSError], None] | None" = None
    bound_loops: bool = False
    check_intermediate_literal: bool = _INTERMEDIATE_LITERAL_IS_CHECKED
    unfollowed_final_literal: bool = _FINAL_LITERAL_IS_NOT_FOLLOWED


def _select(
    path: _Globable,
    steps: _ty.Sequence[_ty.Tuple[str, _ty.Any]],
    index: int,
    opts: _Options,
    is_dir: bool | None,
    entries: "_ty.Sequence | None" = None,
) -> _ty.Iterator[_Globable]:
    """Select from `path` with `steps[index:]`. `entries` is `path`'s listing
    when the caller took it already (see `_scan()`)."""
    part, kind = steps[index]
    last = index == len(steps) - 1

    if kind is None:  # "**"
        if is_dir is None and not path.is_dir():
            _report_unlistable(path, opts.on_error)
            return
        if last:
            with_files = opts.doublestar_selects_files and not opts.dironly
            yield from _recurse(
                path,
                opts.include_hidden,
                with_files,
                opts.on_error,
                opts.bound_loops,
            )
            return
        for directory, listing in _recurse_dirs(
            path, opts.include_hidden, opts.on_error, opts.bound_loops
        ):
            yield from _select(directory, steps, index + 1, opts, True, listing)
        return

    if kind is False:  # literal component: no listing needed
        if (
            index == 0
            and part == ".."
            and opts.check_intermediate_literal
            and not path.is_dir()
        ):
            # pathlib selects nothing from a base that is not a directory,
            # even where the system would resolve "base/.." by its text.
            _report_unlistable(path, opts.on_error)
            return
        child = _child(path, part, None)
        if not last:
            if (
                opts.check_intermediate_literal
                and part not in _SPECIAL_PARTS
                and not child.is_dir()
            ):
                _report_unlistable(child, opts.on_error)
                return
            yield from _select(child, steps, index + 1, opts, None)
        elif opts.dironly:
            if child.is_dir():
                yield child
        elif (
            child.exists(follow_symlinks=False)
            if opts.unfollowed_final_literal
            else child.exists()
        ):
            yield child
        return

    need_dir = opts.dironly or not last
    skip_hidden = not opts.include_hidden and not part.startswith(".")
    for child, stat in _scan(path, opts.on_error) if entries is None else entries:
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
    top: _Globable,
    include_hidden: bool,
    with_files: bool,
    on_error=None,
    bound_loops: bool = False,
) -> _ty.Iterator[_Globable]:
    """Yield `top` and every directory below it (plus every other entry when
    `with_files`), never descending through a directory symlink.

    With `bound_loops`, a directory whose identity (`st_dev`, `st_ino`) is
    already on the CURRENT DESCENT PATH is skipped: that is what a loop is
    -- a directory reachable below itself -- and it bounds a Windows
    junction loop, which no symlink check can see (a junction reports
    `is_symlink() == False`).

    The ancestor chain is the whole rule, not a set of everything seen. One
    directory deliberately reachable under two SIBLING names (a shared
    config layer junctioned in as `site-a` and `site-b`) is not a loop: the
    walk terminates, and both names must expand. A `visited` set spanning
    the traversal dropped the second one silently. `find -L` draws the line
    in the same place.

    A backend whose stat has no identity cannot be bounded this way and is
    walked as before.
    """
    yield top
    top_key = _identity(top) if bound_loops else None
    # Each stack entry carries the identities of the directories the walk is
    # currently inside -- pushed on descent, dropped with the branch.
    stack = [(top, (top_key,) if top_key is not None else ())]
    while stack:
        directory, ancestors = stack.pop()
        for child, stat in _scan(directory, on_error):
            if not include_hidden and child.is_hidden():
                continue
            is_dir = _entry_is_dir(child, stat, follow_symlinks=False)
            child_ancestors = ancestors
            if is_dir:
                descend, child_ancestors = _loop_guard(child, ancestors, bound_loops)
                if not descend:
                    continue
            if is_dir or with_files:
                yield child
            if is_dir:
                stack.append((child, child_ancestors))


def _recurse_dirs(
    top: _Globable,
    include_hidden: bool,
    on_error=None,
    bound_loops: bool = False,
) -> _ty.Iterator[_ty.Tuple[_Globable, _ty.Sequence]]:
    """`_recurse()` over directories only, as `(directory, its listing)`: the
    caller lists each one next, and gets the listing that found the
    directories below it instead of asking the backend again. Same order,
    same loop rule. A directory waiting its turn keeps its subdirectories,
    not its whole listing."""
    entries = _scan(top, on_error)
    yield top, entries
    top_key = _identity(top) if bound_loops else None
    stack = [
        (
            _subdirectories(entries, include_hidden),
            (top_key,) if top_key is not None else (),
        )
    ]
    while stack:
        children, ancestors = stack.pop()
        for child in children:
            descend, child_ancestors = _loop_guard(child, ancestors, bound_loops)
            if not descend:
                continue
            entries = _scan(child, on_error)
            yield child, entries
            stack.append((_subdirectories(entries, include_hidden), child_ancestors))


def _subdirectories(entries, include_hidden: bool) -> "list[_Globable]":
    return [
        child
        for child, stat in entries
        if (include_hidden or not child.is_hidden())
        and _entry_is_dir(child, stat, follow_symlinks=False)
    ]


def _loop_guard(child: _Globable, ancestors: tuple, bound_loops: bool):
    """`(descend, identities of the directories child is inside)`. A child
    that is one of its own ancestors is neither descended into nor yielded:
    the next pattern component would match everything under it a second
    time, through the loop."""
    if not bound_loops:
        return True, ancestors
    key = _identity(child)
    if key is None:
        return True, ancestors
    if key in ancestors:
        return False, ancestors
    return True, ancestors + (key,)


def _report_unlistable(path: _Globable, on_error) -> None:
    """Tell `on_error` why `path` has nothing to select from (a missing path,
    a file), in the words listing it would use."""
    if on_error is not None:
        _scan(path, on_error)


def _scan(directory: _Globable, on_error=None):
    """(child, non-following stat or None) per entry; nothing if listing
    fails. Materialized so a lazily-raising listing is still caught here.

    `on_error`, when given, is called as `on_error(error)` with the failure
    -- the same contract as `Path.walk(on_error=)` and `os.walk`. Raising
    from it propagates, which is how a caller turns an unreadable directory
    back into an error; returning treats it as "nothing here", which is what
    happens with no hook at all. `error.filename` is filled in with the
    directory when the backend left it empty, so one argument is enough to
    say where.
    """
    scandir = getattr(directory, "_scandir", None)
    try:
        if scandir is None:
            return [(child, None) for child in directory.iterdir()]
        return [(_child(directory, name, stat), stat) for name, stat in scandir()]
    except OSError as error:
        if on_error is not None:
            if not getattr(error, "filename", None):
                try:
                    error.filename = str(directory)
                except Exception:
                    pass
            on_error(error)
        return ()


def _identity(path: _Globable) -> "tuple | None":
    """`(st_dev, st_ino)` of what `path` resolves to, or None when the
    backend cannot say. Follows links deliberately: a Windows junction
    reports `is_symlink() == False`, so its own metadata is no help --
    `stat()` returning the TARGET's identity is what makes a loop visible.
    """
    try:
        st = path.stat()
    except (OSError, NotImplementedError, ValueError):
        return None
    dev = getattr(st, "st_dev", None)
    ino = getattr(st, "st_ino", None)
    # `st_ino` is zero where the filesystem reports no file identity; every
    # directory would then share one key and look like its own ancestor.
    if dev is None or not ino:
        return None
    return (dev, ino)


def _child(directory: _Globable, name: str, stat=None) -> _Globable:
    """The child of `directory` called exactly `name`, a name its listing
    returned. A caller that has not checked it with `utils.is_safe_child_name()`
    must, since a `UriPath` joined with `/` resolves `..` and splits at `/`."""
    if getattr(directory, "_pop_stat_hint", None) is not None:
        # UriPath: the name is attached as one segment, and seeding the
        # listing's stat saves a round trip per entry.
        return directory._make_child_relpath(name, stat_hint=stat)
    return directory / name


def _entry_is_dir(path: _Globable, stat, *, follow_symlinks: bool) -> bool:
    if stat is not None and not (follow_symlinks and stat.is_symlink()):
        return stat.is_dir()
    if follow_symlinks:
        return path.is_dir()
    return path.is_dir() and not path.is_symlink()
