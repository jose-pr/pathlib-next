from __future__ import annotations

import enum as _enum
import errno as _errno
import logging as _logging
import pathlib as _pathlib
import typing as _ty
import uuid as _uuid

from .. import utils as _utils
from ..mempath import MemPath as _MemPath
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

    `Uri.is_local()` does a real (`lru_cache`d) hostname resolution
    (`Source.is_local()`, see `uri/source.py`, backed by
    `netimps.resolve()` since 2026-07-29) -- a hostname that genuinely
    doesn't resolve anywhere correctly returns `False` (non-local), not an
    exception, but a transport-level failure in the resolver chain itself
    (all backends erroring, e.g. no network at all) can still raise
    `OSError`. Treated as "local" on that exception for the same
    safe-default reason as the "no `is_local()` method" case above:
    quick_check simply doesn't kick in, falling through to a real checksum
    comparison exactly like pre-`quick_check` behavior, rather than
    letting an unrelated resolver failure crash the sync outright.
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

    An `st_mtime` of 0 (or missing) on either side is "unknown", never a
    timestamp: MemPath, GitHub/GitLab, `data:` and HTTP without
    Last-Modified all report 0, and two unknowns are not a match.
    """
    source_stat, target_stat = source.stat, target.stat
    if source_stat is None or target_stat is None:
        return False
    if not source_stat.st_mtime or not target_stat.st_mtime:
        return False
    return (
        source_stat.st_size == target_stat.st_size
        and source_stat.st_mtime == target_stat.st_mtime
    )


#: Set on an exception once `ignore_error` has declined it, so every
#: enclosing hook re-raises it instead of consulting the policy again with an
#: ancestor's paths.
_OFFERED_ATTR = "_pathlib_next_sync_offered"


def _offer_error(policy, error: Exception, source, target, event) -> bool:
    """Ask `policy` about `error` once. True means "tolerated". An error the
    policy already declined (deeper in the tree) is re-raised by every outer
    level without asking again."""
    if getattr(error, _OFFERED_ATTR, False):
        return False
    if policy(error, source, target, event):
        return True
    try:
        setattr(error, _OFFERED_ATTR, True)
    except Exception:
        pass
    return False


def _implements(path: Path, name: str) -> bool:
    """Whether `path`'s class overrides the base `Path` stub `name` (a
    `@notimplemented` method). Decided on the class, before anything is
    touched; a backend that implements the primitive can still fail at
    runtime."""
    return getattr(type(path), name, None) is not getattr(Path, name, None)


def _supports_symlinks(path: Path) -> bool:
    return _implements(path, "_symlink_to") or _implements(path, "symlink_to")


def _temp_sibling(path: Path) -> Path:
    # Hidden and unique, in the same directory so a rename stays on one
    # filesystem/connection. A leftover from a crash is not in the source,
    # so the next remove_missing run deletes it.
    return path.with_name(
        f".{_child_name(path)}.{_uuid.uuid4().hex[:12]}.pathlib-next-tmp"
    )


def _replace(source: Path, target: Path) -> None:
    """Rename `source` over the existing `target`. `os.replace` locally
    (atomic on POSIX and Windows); elsewhere `rename()`, which replaces on
    backends with POSIX rename semantics, and otherwise refuses -- then the
    old target is removed first, after the new content is complete."""
    if isinstance(source, _pathlib.Path):
        source.replace(target)
        return
    try:
        source.rename(target)
    except OSError:
        if FileStat.from_path(target, follow_symlink=False) is None:
            raise
        target.unlink()
        source.rename(target)


def _child_name(path: Path) -> str:
    # A directory child of a `UriPath` can end in "/", leaving `.name`
    # empty; its last real component is then the parent's name.
    return path.name or path.parent.name


def _paths_overlap(source: Path, target: Path) -> bool:
    """Whether `source` and `target` are the same tree or one contains the
    other. Only decided for two paths of the same implementation: different
    implementations (e.g. `LocalPath` vs `FileUri`) are never reported.
    `MemPath` equality ignores the backend, so two trees on different
    `MemPathBackend`s are not overlapping even with equal segments.
    `LocalPath` is resolved first so a symlink cannot hide the overlap."""
    if type(source) is not type(target):
        return False
    if isinstance(source, _MemPath) and source.backend is not target.backend:
        return False
    # Equal URIs reached through two distinct explicit backends (separate
    # connections, or fakes standing in for two hosts) are not provably the
    # same tree; only refuse what is.
    source_backend = getattr(source, "_backend", None)
    target_backend = getattr(target, "_backend", None)
    if (
        source_backend is not None
        and target_backend is not None
        and source_backend is not target_backend
    ):
        return False
    if isinstance(source, _pathlib.Path):
        resolved = []
        for path in (source, target):
            try:
                path = path.resolve()
            except (OSError, RuntimeError):
                pass
            # Python < 3.10 on Windows returns a relative path unchanged
            # when no part of it exists.
            resolved.append(path if path.is_absolute() else path.absolute())
        source, target = resolved
    try:
        return target.is_relative_to(source) or source.is_relative_to(target)
    except (TypeError, ValueError, NotImplementedError):
        return False


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
    Symlink = _enum.auto()
    #: Comparing a source/target file pair (quick check or checksum)
    #: failed. Only ever passed to `ignore_error`; no hook fires for it.
    Compare = _enum.auto()
    #: The source entry is neither a regular file, a directory nor a
    #: symlink (a FIFO, socket or device, or a stat with no file type): it is
    #: skipped and the target is left untouched.
    Skipped = _enum.auto()


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
        return self.stat is not None

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
    `SftpPath`'s `check-file-handle` support) over streaming the file
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
    correctness regression. An `st_mtime` of 0 on either side means
    "unknown" and never matches. Local-to-local pairs always skip this
    pre-check (unchanged pre-existing behavior -- local reads are already
    cheap, and this project's `copy(preserve_metadata=True)` doesn't
    guarantee mtime propagation on every path, see `docs/divergences.md`).
    Set `quick_check=False` to disable the pre-check entirely and always
    checksum, matching pre-`quick_check` behavior for non-local pairs too.

    `follow_symlinks` (default `True`) controls whether a symlink source is
    resolved during traversal (content synced as if it weren't a link) or
    reported as a symlink (`is_symlink()` true). When it's `False` and a
    symlink source is reached, `symlink_mode` decides what happens:
    `"preserve"` (default) creates a matching symlink on `target` with the
    same raw, unresolved target string `readlink()` returned (dangling
    links and relative targets included -- never resolved/validated);
    `"reject"` raises `NotImplementedError` instead (the only behavior
    before this kwarg existed). If `target` can't create symlinks at all
    (most backends -- only `LocalPath` and `SftpPath` currently implement
    `symlink_to()`), `"preserve"` mode raises `NotImplementedError` too,
    through the same `ignore_error`/`hook()` machinery as every other
    branch -- decided before an existing target entry is touched. Replacing
    an existing entry creates the new link under a temporary sibling name
    first, so a runtime refusal also leaves the entry in place.

    A changed file is written to a temporary sibling and renamed over the
    existing target where the target backend implements `rename()`, so a
    failed or interrupted transfer keeps the previous version. A backend
    without `rename()` is overwritten in place, with the source opened
    before the target is truncated. A source entry that is not a regular
    file, directory or symlink (FIFO, socket, device) is skipped with a
    `SyncEvent.Skipped` event and its target left untouched.

    Errors: `ignore_error` is consulted once per error, with the paths of
    the entry that failed and the event that failed (`SyncEvent.Compare`
    for a failing quick check or checksum). An error it declines propagates
    without being offered again by enclosing directories. A dry run takes
    the same decisions as a real run (a directory that would be created is
    treated as empty) without changing anything.
    """

    __slots__ = (
        "checksum",
        "_hook",
        "remove_missing",
        "follow_symlinks",
        "symlink_mode",
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
        symlink_mode: '_ty.Literal["preserve", "reject"]' = "preserve",
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
        if symlink_mode not in ("preserve", "reject"):
            raise ValueError(
                f"symlink_mode must be 'preserve' or 'reject', got {symlink_mode!r}"
            )
        self.symlink_mode = symlink_mode
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
                if _offer_error(ignore_error, e, source, target, event):
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
                    # `None` means "stat unknown" (GitLab blobs, FTP's NLST
                    # fallback), not "missing": ask the path itself before
                    # remove_missing can treat the entry as gone.
                    if stat is None:
                        stat = FileStat.from_path(
                            child, follow_symlink=self.follow_symlinks
                        )
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

        The root call is checked before anything is touched: a `source`
        that does not exist raises `FileNotFoundError` (only a child that
        vanishes mid-sync takes the `remove_missing` path), and a `source`
        and `target` of the same implementation where one contains the
        other raise `ValueError`. Both go through `ignore_error`; a
        tolerated error ends the call without changes. Child names that
        would not stay a single component inside `target` (`..`, a
        separator or drive on a Windows target) raise `ValueError` the
        same way, and symlinks found inside `target` are replaced, never
        written, listed or deleted through.
        """
        _ignore_error = (
            self.ignore_error
            if ignore_error is None
            else _utils.as_error_handler(ignore_error)
        )
        return self._sync(source, target, dry_run, _ignore_error, True)

    def _sync(
        self,
        source: Path | PathAndStat,
        target: Path | PathAndStat,
        dry_run: bool,
        _ignore_error: _OnPathSyncerError,
        root: bool,
    ):
        checksum = self.checksum

        def start():
            nonlocal source, target
            source = (
                PathAndStat(source, follow_symlink=self.follow_symlinks)
                if not isinstance(source, PathAndStat)
                else source
            )
            # `follow_symlinks` describes the SOURCE traversal. Below the
            # root, a target entry is always lstat'd so a link inside the
            # destination is replaced rather than followed out of it. The
            # root target is the one the caller named, and keeps the
            # caller's setting.
            target = (
                PathAndStat(
                    target, follow_symlink=self.follow_symlinks if root else False
                )
                if not isinstance(target, PathAndStat)
                else target
            )

        if self.hook(source, target, SyncEvent.SyncStart, False, start, _ignore_error):
            return

        if root:
            error = None
            if not source.exists():
                # A typo, unmounted share or HTTP 404 must not read as "an
                # empty source" and wipe the target under remove_missing.
                error = FileNotFoundError(
                    _errno.ENOENT, "sync source does not exist", str(source.path)
                )
            elif _paths_overlap(source.path, target.path):
                error = ValueError(
                    f"cannot sync {source.path} onto {target.path}: "
                    "source and target overlap"
                )
            if error is not None:
                if not _offer_error(
                    _ignore_error, error, source, target, SyncEvent.SyncStart
                ):
                    raise error
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
            if self.symlink_mode == "reject":
                error = NotImplementedError("symlink sync not implemented yet")
                if not _offer_error(
                    _ignore_error, error, source, target, SyncEvent.Symlink
                ):
                    raise error
                return

            def create_symlink():
                # Decided before the target is touched: a backend without
                # symlinks used to lose the existing entry and then fail.
                if not _supports_symlinks(target.path):
                    raise NotImplementedError(
                        "symlink_to() not supported by " f"{type(target.path).__name__}"
                    )
                # Raw, unresolved target string. A Uri's `.path` is the
                # un-prefixed link text (Uri.as_posix() prepends "host:" when
                # the result carries a host, e.g. SftpPath's absolute
                # targets); plain stdlib Path has no `.path`, so
                # `.as_posix()` is the only accessor there. Never resolved
                # against source's parent -- a relative target stays
                # relative either way.
                link = source.path.readlink()
                raw_target = link.path if hasattr(link, "path") else link.as_posix()

                if not target.exists() and not target.is_symlink():
                    target.path.symlink_to(raw_target)
                    return
                # Type mismatch or a stale link: create the new link under a
                # temporary sibling first, so a runtime refusal (no symlink
                # privilege on Windows, a server permission error) fails
                # before the existing entry is removed.
                temp = _temp_sibling(target.path)
                temp.symlink_to(raw_target)
                try:
                    if target.is_file() or target.is_symlink():
                        target.path.unlink()
                    else:
                        target.path.rm(recursive=target.is_dir())
                    try:
                        temp.rename(target.path)
                    except NotImplementedError:
                        target.path.symlink_to(raw_target)
                finally:
                    try:
                        if FileStat.from_path(temp, follow_symlink=False):
                            temp.unlink()
                    except Exception:
                        pass

            if self.hook(
                source,
                target,
                SyncEvent.Symlink,
                dry_run,
                create_symlink,
                _ignore_error,
            ):
                return
        elif source.is_file():
            synced = False
            if target.is_file():

                def compare():
                    # quick_check: cheap metadata-only pre-check for
                    # non-local pairs (see class docstring) -- a match skips
                    # checksumming entirely; a mismatch always falls through
                    # to a real checksum comparison, never concludes
                    # "changed" on its own.
                    if (
                        self.quick_check
                        and (not _is_local(source.path) or not _is_local(target.path))
                        and _quick_check_in_sync(source, target)
                    ):
                        return True
                    if checksum is _default_checksum:
                        # Route through the paired native-vs-streaming
                        # policy (see class docstring) instead of two
                        # independent single-path calls -- only this branch
                        # can coordinate "both native or both streamed".
                        return _default_checksums_match(source, target)
                    return checksum(target) == checksum(source)

                # Reported against this file pair, once: outside a hook an
                # error here reached the policy only through every ancestor
                # directory's hooks (and never for a root file pair).
                try:
                    synced = compare()
                except Exception as error:
                    if _offer_error(
                        _ignore_error, error, source, target, SyncEvent.Compare
                    ):
                        return
                    raise
            if not synced:

                def copy():
                    if target.is_file() and _implements(target.path, "rename"):
                        # Write the new content beside the target and rename
                        # it over: a failed or interrupted transfer leaves
                        # the previous version in place.
                        temp = _temp_sibling(target.path)
                        try:
                            source.path.copy(temp)
                            _replace(temp, target.path)
                        except BaseException:
                            try:
                                temp.unlink(missing_ok=True)
                            except Exception:
                                pass
                            raise
                    elif target.is_file():
                        # No rename: Path.copy() opens the source before it
                        # truncates the target, so a missing or unreadable
                        # source still leaves the target intact.
                        source.path.copy(target.path, overwrite=True)
                    else:
                        if target.is_symlink():
                            target.path.unlink()
                        elif target.exists():
                            target.path.rm(recursive=target.is_dir())
                        source.path.copy(target.path)

                if self.hook(
                    source, target, SyncEvent.Copy, dry_run, copy, _ignore_error
                ):
                    return
        elif source.is_dir():
            # A symlink inside the destination is replaced by a real
            # directory: listing, writing or removing through it would act
            # on whatever it points at. unlink() removes only the link.
            # Any other non-directory (a FIFO, socket or device) is replaced
            # the same way.
            if (target.is_symlink() and not root) or (
                target.exists() and not target.is_dir() and not target.is_symlink()
            ):
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

            # A directory that did not exist (or was only just created, or
            # in a dry run only would have been) has no children: nothing to
            # list or remove, and every child target is known to be missing.
            # Listing it anyway crashed every dry run over a new directory.
            target_absent = not target.exists()
            if target_absent:
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
            windows_target = _utils.is_windows_flavoured(target.path)

            def unsafe_name(name, source_entry, target_entry, event):
                # Names come from a listing the destination does not
                # control; one that is not a single component inside
                # `target` ("..", or "\\"/":" on a Windows target) must
                # never be joined onto it. Reported, not silently skipped.
                if _utils.is_safe_child_name(name, windows=windows_target):
                    return False
                error = ValueError(
                    f"refusing unsafe child name {name!r} under {target.path}"
                )
                if not _offer_error(
                    _ignore_error, error, source_entry, target_entry, event
                ):
                    raise error
                return True

            def get_source_children():
                nonlocal source_children
                if source_children is None:
                    source_children = self._children(source)
                return source_children

            if self.remove_missing and not target_absent:

                def checkchildren():
                    source_names = {child.path.name for child in get_source_children()}
                    for child in self._children(target):

                        def checkchild():
                            if unsafe_name(
                                _child_name(child.path),
                                source,
                                child,
                                SyncEvent.RemovedMissing,
                            ):
                                return
                            if child.path.name not in source_names:
                                # The event describes the entry removed, not
                                # the directory being checked.
                                self.hook(
                                    PathAndStat.from_stat(
                                        source.path / _child_name(child.path), None
                                    ),
                                    child,
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
                    name = _child_name(child.path)
                    if unsafe_name(name, child, target, SyncEvent.SyncChild):
                        continue
                    self.hook(
                        source,
                        target,
                        SyncEvent.SyncChild,
                        False,
                        # Propagate the resolved policy into the recursive
                        # call so a per-call override applies to the whole
                        # subtree, not just this level.
                        lambda child=child, name=name: self._sync(
                            child,
                            (
                                PathAndStat.from_stat(target.path / name, None)
                                if target_absent
                                else target.path / name
                            ),
                            dry_run,
                            _ignore_error,
                            False,
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
        else:
            # Not a file, directory or symlink: a FIFO, socket or device (or
            # a stat without a file type). Nothing sensible can be copied,
            # and treating it as a directory replaced a same-named target
            # file with an empty directory and then failed to list it.
            self.hook(source, target, SyncEvent.Skipped, dry_run, None, _ignore_error)
            return

        self.hook(source, target, SyncEvent.Synced, dry_run, None, _ignore_error)
