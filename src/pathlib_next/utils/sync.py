from __future__ import annotations

import enum as _enum
import logging as _logging
import typing as _ty

from .. import utils as _utils
from ..path import Path
from ..utils.stat import FileStat
from .checksum import md5 as _md5

_logger = _logging.getLogger("pathlib_next.sync")


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
    branch.
    """

    __slots__ = (
        "checksum",
        "_hook",
        "remove_missing",
        "follow_symlinks",
        "symlink_mode",
        "ignore_error",
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
    ) -> None:
        if checksum is None:
            checksum = lambda entry: _md5(entry.path)
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
            if self.symlink_mode == "reject":
                error = NotImplementedError("symlink sync not implemented yet")
                if not _ignore_error(error, source, target, SyncEvent.Symlink):
                    raise error
                return

            def create_symlink():
                # Raw, unresolved target string -- readlink() returns a
                # Path-like object on every implementation that has it
                # (stdlib Path, or SftpPath's `with_segments(target)`, a
                # Uri carrying the *source's* host/scheme). Uri.as_posix()
                # prepends "host:" (or "user@host:") when a source/host is
                # present, which corrupts a bare relative/absolute symlink
                # target (e.g. "real.txt" -> "host:real.txt") -- so Uri's
                # own `.path` (the raw, un-prefixed path string, no host)
                # is used when available; plain stdlib Path has no `.path`
                # attribute at all, so `.as_posix()` is the correct and
                # only accessor there. Never resolved against source's
                # parent -- a relative target stays relative either way.
                link = source.path.readlink()
                raw_target = link.path if hasattr(link, "path") else link.as_posix()

                # Type mismatch: target exists as something other than a
                # symlink (file or dir) -- clear it first, same pattern as
                # the Copy branch above.
                if target.is_file() or target.is_symlink():
                    target.path.unlink()
                elif target.exists():
                    target.path.rm(recursive=target.is_dir())

                symlink_to = getattr(target.path, "symlink_to", None)
                if symlink_to is None:
                    # target backend has no symlink_to() at all (e.g.
                    # MemPath, HttpPath) -- normalize to the same
                    # NotImplementedError reject mode raises, so
                    # ignore_error/hook() callers see one consistent
                    # error shape regardless of *why* symlink creation
                    # isn't possible. A backend that DOES define
                    # symlink_to() but itself raises NotImplementedError
                    # (e.g. a future @_utils.notimplemented stub) is left
                    # to propagate its own error unchanged -- only the
                    # "attribute doesn't exist at all" case is normalized
                    # here.
                    raise NotImplementedError(
                        "symlink_to() not supported by "
                        f"{type(target.path).__name__}"
                    )
                symlink_to(raw_target)

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
                if checksum(target) == checksum(source):
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
