from __future__ import annotations

import enum as _enum
import logging as _logging
import typing as _ty

from .. import utils as _utils
from ..path import Path
from ..utils.stat import FileStat
from . import checksum as _checksum

_logger = _logging.getLogger("pathlib_next.sync")

#: Sentinel identifying `PathSyncer`'s own default checksum policy (native
#: digest preferred, streaming fallback) so `sync()` can special-case it --
#: a caller-supplied `checksum` callable is always invoked exactly as
#: before (single path in, value out, compared with `==`). See
#: `_default_checksums_match()`.
_DEFAULT_ALGORITHM = "md5"


def _default_checksum(entry: "PathAndStat") -> str:
    # Kept for backward compatibility: `PathSyncer().checksum` must still
    # be a plain `Callable[[PathAndStat], Any]`, e.g. for callers that read
    # `.checksum` directly rather than going through `sync()`. `sync()`
    # itself never calls this for the default policy -- it calls
    # `_default_checksums_match()` instead, which can coordinate the
    # native-vs-streaming decision across BOTH sides at once (a single-path
    # function like this one structurally cannot).
    native = _checksum.native(entry.path, _DEFAULT_ALGORITHM)
    if native is not None:
        return native
    return _checksum.stream(entry.path, _DEFAULT_ALGORITHM)


def _shared_native_algorithm(source: Path, target: Path) -> "str | None":
    """Pick an algorithm both sides advertise via
    `NativeChecksum.supported_checksums()` (see `protocols/checksum.py`),
    preferring `_DEFAULT_ALGORITHM` ("md5") when both sides support it --
    matches `PathSyncer`'s pre-existing default algorithm, so the common
    case (both sides only ever supported md5) picks the same algorithm as
    before this helper existed. Returns `None` if either side has no
    `supported_checksums` at all (a `NativeChecksum` implementation isn't
    required to override the advisory method -- the base default already
    returns `frozenset()`, indistinguishable here from "doesn't implement
    the protocol"), or if the two sides' advertised sets don't overlap.
    """
    source_supported = getattr(source, "supported_checksums", None)
    target_supported = getattr(target, "supported_checksums", None)
    if source_supported is None or target_supported is None:
        return None
    shared = source_supported() & target_supported()
    if not shared:
        return None
    if _DEFAULT_ALGORITHM in shared:
        return _DEFAULT_ALGORITHM
    return next(iter(shared))


def _default_checksums_match(source: "PathAndStat", target: "PathAndStat") -> bool:
    """`PathSyncer`'s default in-sync check: prefer each side's
    `NativeChecksum.checksum()` (no network transfer needed just to
    *decide* whether a copy is needed), but only trust a native digest from
    one side if the OTHER side can also produce a digest under the exact
    same algorithm -- native or streamed. Mixing "native digest from A" with
    "streamed digest from B" would only be numerically safe if both are
    guaranteed true content hashes under the same algorithm; the protocol
    contract for `NativeChecksum.checksum()` already guarantees that
    (`NotImplementedError` on any doubt, e.g. S3 multipart ETags), so this
    conservative both-native-or-both-streamed policy is a deliberate
    simplicity choice, not a soundness requirement -- see
    `protocols/checksum.py`.

    Algorithm selection: try `supported_checksums()` intersection first
    (`_shared_native_algorithm`) -- avoids a doomed native attempt when the
    two sides' capabilities don't overlap on `_DEFAULT_ALGORITHM`. Falls
    back to trying `_DEFAULT_ALGORITHM` directly (the pre-`supported_checksums`
    behavior) when either side doesn't implement the advisory method at
    all, since a `NativeChecksum` implementation is never required to
    override it.
    """
    algorithm = _shared_native_algorithm(source.path, target.path) or _DEFAULT_ALGORITHM
    source_native = _checksum.native(source.path, algorithm)
    target_native = _checksum.native(target.path, algorithm)
    if source_native is not None and target_native is not None:
        return source_native == target_native
    return _checksum.stream(source.path, _DEFAULT_ALGORITHM) == _checksum.stream(
        target.path, _DEFAULT_ALGORITHM
    )


def _is_local(path) -> bool:
    """`quick_check`'s locality test. `Uri.is_local()` (`uri/__init__.py`)
    exists only on `Uri`/`UriPath` -- a plain `LocalPath` or `MemPath` has
    no such method at all. Both of those ARE effectively local for this
    purpose (a real local filesystem, or an in-memory structure with no
    network cost either way), so "no `is_local()` method" is treated as
    local -- the safe default, since it only preserves the pre-`quick_check`
    always-checksum behavior rather than skipping a comparison it shouldn't.

    `Uri.is_local()` does a real (`lru_cache`d) DNS lookup
    (`Source.is_local()`, see `uri/source.py`) -- a hostname that doesn't
    resolve at all (unreachable, fake/test host, transient DNS hiccup)
    raises `socket.gaierror`, not just returns `False`. Treated the same
    as "local" here for the same safe-default reason: quick_check simply
    doesn't kick in, falling through to a real checksum comparison exactly
    like pre-`quick_check` behavior, rather than letting an unrelated DNS
    failure crash the sync outright.
    """
    is_local = getattr(path, "is_local", None)
    if is_local is None:
        return True
    try:
        return is_local()
    except OSError:
        return True


def _quick_check_in_sync(source: "PathAndStat", target: "PathAndStat") -> bool:
    """The rsync-style "quick check" pre-check: True only when BOTH
    `st_size` and `st_mtime` already match between the two cached stats --
    metadata `PathAndStat` already carries from listing, no extra round
    trip. Never used to conclude "out of sync" (a caller must fall through
    to a real checksum comparison on any mismatch) -- see `PathSyncer`'s
    class docstring for why.
    """
    source_stat, target_stat = source.stat, target.stat
    if source_stat is None or target_stat is None:
        return False
    return (
        source_stat.st_size == target_stat.st_size
        and source_stat.st_mtime == target_stat.st_mtime
    )


class SyncEvent(_enum.Enum):
    """Events `PathSyncer.hook()` fires during a sync, for progress/logging
    callbacks."""

    Copy = _enum.auto()
    RemovedMissing = _enum.auto()
    Synced = _enum.auto()
    CreatedDirectory = _enum.auto()
    SyncStart = _enum.auto()
    TypeMismatch = _enum.auto()
    CheckTargetChild = _enum.auto()
    CheckTargetChildren = _enum.auto()
    SyncChild = _enum.auto()
    SyncChildren = _enum.auto()


class PathAndStat(object):
    """A `Path` plus its cached `stat()` result (`None` if it doesn't
    exist). `is_*` attribute access (e.g. `.is_file()`) delegates to the
    cached stat, returning a false-returning callable if the path doesn't
    exist; any other unknown attribute raises `AttributeError` as normal."""

    __slots__ = ("_path", "_stat")

    def __init__(self, path: Path, *, follow_symlink=None) -> None:
        self._path = path
        self.refresh(follow_symlink)

    @classmethod
    def from_stat(cls, path: Path, stat: FileStat | None) -> "PathAndStat":
        entry = cls.__new__(cls)
        entry._path = path
        entry._stat = stat
        return entry

    def __str__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return str((self.path, self._stat))

    @property
    def path(self):
        return self._path

    @property
    def stat(self):
        return self._stat

    def exists(self):
        return self.stat != None

    def refresh(self, follow_symlink: bool):
        self._stat = FileStat.from_path(self.path, follow_symlink=follow_symlink)

    def __getattr__(self, name: str):
        if name.startswith("is_"):
            if self.stat:
                return getattr(self.stat, name)
            else:
                return lambda *args, **kwargs: False
        raise AttributeError(name)


if _ty.TYPE_CHECKING:

    class PathAndStat(PathAndStat, FileStat): ...


class _OnPathSyncerError(_ty.Protocol):
    def __call__(
        self,
        error: Exception,
        source: PathAndStat,
        target: PathAndStat,
        event: SyncEvent,
    ) -> bool: ...


class PathSyncer(object):
    """One-way checksum-driven tree sync: copies/creates in `target`
    whatever differs from `source` (by `checksum`), optionally removing
    files in `target` that are missing from `source`. Works across any two
    `Path` implementations (e.g. `MemPath` -> `LocalPath`, or between two
    `UriPath` schemes) -- see `sync()`.

    The default `checksum` policy prefers each side's backend-native
    digest (`protocols.checksum.NativeChecksum.checksum()`, e.g.
    `SftpPath`'s `check-file@openssh.com` support) over streaming the file
    through `open("rb")`, but only when BOTH sides can produce a digest
    under the same algorithm -- native or streamed. If either side can't
    (missing the protocol, or it raises `NotImplementedError` for the
    requested algorithm), both sides fall back to streaming rather than
    comparing a native digest to a streamed one. A custom `checksum`
    callable disables this native-preferring behavior entirely (it is
    called exactly as before, once per side, compared with `==`).

    `quick_check=True` (the default) adds a cheap metadata-only
    pre-check -- the classic rsync "quick check" heuristic -- for any pair
    where at least one side is non-local (`Uri.is_local()`; a side without
    an `is_local()` method at all, e.g. plain `LocalPath`/`MemPath`, is
    treated as local): if `st_size` AND `st_mtime` already match (from the
    listing/stat metadata `PathAndStat` already carries -- no extra round
    trip), the pair is treated as in sync WITHOUT calling `checksum` at
    all, native or streamed. A mismatch on either falls through to a real
    checksum comparison rather than being treated as "changed" -- mtime can
    be unreliable across backends/clock skew, so a false "needs copy" from
    a mismatch is merely wasteful, while a false "in sync" would be a
    correctness regression. Local-to-local pairs always skip this
    pre-check (unchanged pre-existing behavior -- local reads are already
    cheap, and this project's `copy(preserve_metadata=True)` doesn't
    guarantee mtime propagation on every path, see `docs/divergences.md`).
    Set `quick_check=False` to disable the pre-check entirely and always
    checksum, matching pre-`quick_check` behavior for non-local pairs
    too."""

    __slots__ = (
        "checksum",
        "_hook",
        "remove_missing",
        "follow_symlinks",
        "ignore_error",
        "quick_check",
    )
    EVENT_LOG_FORMAT = "[%s] Source:%s Target:%s DryRun:%s"

    def __init__(
        self,
        checksum: _ty.Callable[[PathAndStat], _ty.Any] | None = None,
        /,
        remove_missing: bool = False,
        follow_symlinks: bool = True,
        hook: _ty.Callable[[PathAndStat, PathAndStat, SyncEvent, bool], None] = None,
        ignore_error: _OnPathSyncerError | bool = False,
        quick_check: bool = True,
    ) -> None:
        # `None` (the default) resolves to `_default_checksum` -- a sentinel
        # `sync()` recognizes (via `is`) to route through
        # `_default_checksums_match()` instead of two independent calls, so
        # the native-vs-streaming decision can be coordinated across BOTH
        # sides at once. A caller-supplied callable is stored and used
        # as-is (`checksum(target) == checksum(source)`, unchanged from
        # before this feature).
        if checksum is None:
            checksum = _default_checksum
        self.checksum = checksum
        self.remove_missing = remove_missing
        self._hook = hook
        self.follow_symlinks = follow_symlinks
        self.ignore_error = _ty.cast(
            _OnPathSyncerError, _utils.as_error_handler(ignore_error)
        )
        self.quick_check = quick_check

    def log(self, msg: str, *args: object):
        # Overridable hook: subclasses/instances may reassign `log` (or
        # subclass) to route sync progress elsewhere. `*args` are passed
        # to the logger lazily (stdlib %-style) so formatting is skipped
        # entirely unless something is actually listening at INFO.
        _logger.info(msg, *args)

    def hook(
        self,
        source: PathAndStat,
        target: PathAndStat,
        event: SyncEvent,
        dry_run: bool,
        do: _ty.Callable[[], None] = None,
        ignore_error: _OnPathSyncerError = None,
    ):
        # `ignore_error` lets sync() pass down a per-call policy override;
        # None keeps the instance-level policy (the only behavior before).
        if ignore_error is None:
            ignore_error = self.ignore_error
        if not dry_run and do:
            try:
                do()
            except Exception as e:
                if ignore_error(e, source, target, event):
                    return e
                raise
        if self._hook:
            self._hook(source, target, event, dry_run)
        self.log(self.EVENT_LOG_FORMAT, event, source, target, dry_run)

    def _children(self, entry: PathAndStat) -> "list[PathAndStat]":
        children: "list[PathAndStat]" = []
        for scan_entry in entry.path._scandir():
            if isinstance(scan_entry, tuple) and len(scan_entry) == 2:
                name, stat = scan_entry
                child = entry.path / name
                if self.follow_symlinks:
                    children.append(
                        PathAndStat(child, follow_symlink=self.follow_symlinks)
                    )
                else:
                    children.append(PathAndStat.from_stat(child, stat))
                continue

            child = entry.path / scan_entry.name
            try:
                stat = FileStat.from_stat(
                    scan_entry.stat(follow_symlinks=self.follow_symlinks)
                )
            except FileNotFoundError:
                stat = None
            children.append(PathAndStat.from_stat(child, stat))
        return children

    def sync(
        self,
        source: Path | PathAndStat,
        target: Path | PathAndStat,
        /,
        dry_run: bool = False,
        ignore_error: _OnPathSyncerError | bool | None = None,
    ):
        """Sync `source` onto `target`.

        `ignore_error` overrides the instance-level policy for this call
        only. It accepts a bool or a callable with the same
        `(error, source, target, event)` arity as the constructor's; `None`
        (the default) means "use the policy given to `__init__`".

        The default used to be the bool `False`, which both shadowed a
        constructor-supplied policy and was *called* directly by the symlink
        branch (`TypeError: 'bool' object is not callable`). Passing a
        callable explicitly behaves exactly as before.
        """
        checksum = self.checksum
        _ignore_error = (
            self.ignore_error
            if ignore_error is None
            else _utils.as_error_handler(ignore_error)
        )

        def start():
            nonlocal source, target
            source = (
                PathAndStat(source, follow_symlink=self.follow_symlinks)
                if not isinstance(source, PathAndStat)
                else source
            )
            target = (
                PathAndStat(target, follow_symlink=self.follow_symlinks)
                if not isinstance(target, PathAndStat)
                else target
            )

        if self.hook(source, target, SyncEvent.SyncStart, False, start, _ignore_error):
            return

        if not source.exists():
            if self.remove_missing:
                if self.hook(
                    source,
                    target,
                    SyncEvent.RemovedMissing,
                    dry_run,
                    lambda: target.path.rm(recursive=True, missing_ok=True),
                    _ignore_error,
                ):
                    return
        elif source.is_symlink():
            error = NotImplementedError("symlink sync not implemented yet")
            # Was `ignore_error(...)` -- the raw parameter, whose default was
            # the bool False, so this raised TypeError instead of honoring
            # (or reporting) the policy. Use the resolved callable.
            if not _ignore_error(error, source, target, None):
                raise error
            return
        elif source.is_file():
            synced = False
            if target.is_file():
                # quick_check: cheap metadata-only pre-check for non-local
                # pairs (see class docstring) -- a match skips checksumming
                # entirely; a mismatch always falls through to a real
                # checksum comparison, never concludes "changed" on its
                # own.
                quick_matched = (
                    self.quick_check
                    and (not _is_local(source.path) or not _is_local(target.path))
                    and _quick_check_in_sync(source, target)
                )
                if quick_matched:
                    matches = True
                elif checksum is _default_checksum:
                    # Route through the paired native-vs-streaming policy
                    # (see class docstring) instead of two independent
                    # single-path calls -- only this branch can coordinate
                    # "both native or both streamed" across both sides.
                    matches = _default_checksums_match(source, target)
                else:
                    matches = checksum(target) == checksum(source)
                if matches:
                    synced = True
            if not synced:

                def copy():
                    if target.is_file() or target.is_symlink():
                        target.path.unlink()
                    else:
                        if target.exists():
                            target.path.rm(recursive=target.is_dir())
                    source.path.copy(target.path)

                if self.hook(
                    source, target, SyncEvent.Copy, dry_run, copy, _ignore_error
                ):
                    return
        else:
            if target.is_file():
                if self.hook(
                    source,
                    target,
                    SyncEvent.TypeMismatch,
                    dry_run,
                    lambda: target.path.unlink(),
                    _ignore_error,
                ):
                    return

                target._stat = None

            if not target.exists():
                if self.hook(
                    source,
                    target,
                    SyncEvent.CreatedDirectory,
                    dry_run,
                    lambda: target.path.mkdir(),
                    _ignore_error,
                ):
                    return

            source_children = None

            def get_source_children():
                nonlocal source_children
                if source_children is None:
                    source_children = self._children(source)
                return source_children

            if self.remove_missing:

                def checkchildren():
                    source_names = {child.path.name for child in get_source_children()}
                    for child in self._children(target):

                        def checkchild():
                            if child.path.name not in source_names:
                                self.hook(
                                    source,
                                    target,
                                    SyncEvent.RemovedMissing,
                                    dry_run,
                                    lambda child=child: child.path.rm(recursive=True),
                                    _ignore_error,
                                )

                        self.hook(
                            source,
                            target,
                            SyncEvent.CheckTargetChild,
                            False,
                            checkchild,
                            _ignore_error,
                        )

                self.hook(
                    source,
                    target,
                    SyncEvent.CheckTargetChildren,
                    False,
                    checkchildren,
                    _ignore_error,
                )

            def sync_children():
                for child in get_source_children():
                    self.hook(
                        source,
                        target,
                        SyncEvent.SyncChild,
                        False,
                        # Propagate the resolved policy into the recursive
                        # call so a per-call override applies to the whole
                        # subtree, not just this level.
                        lambda child=child: self.sync(
                            child,
                            target.path / (child.path.name or child.path.parent.name),
                            dry_run,
                            _ignore_error,
                        ),
                        _ignore_error,
                    )

            self.hook(
                source,
                target,
                SyncEvent.SyncChildren,
                False,
                sync_children,
                _ignore_error,
            )

        self.hook(source, target, SyncEvent.Synced, dry_run, None, _ignore_error)
