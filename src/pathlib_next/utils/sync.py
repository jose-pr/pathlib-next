from __future__ import annotations

import enum as _enum
import logging as _logging
import typing as _ty

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
    `UriPath` schemes) -- see `sync()`."""

    __slots__ = (
        "checksum",
        "_hook",
        "remove_missing",
        "follow_symlinks",
        "ignore_error",
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
    ) -> None:
        if checksum is None:
            checksum = lambda entry: _md5(entry.path)
        self.checksum = checksum
        self.remove_missing = remove_missing
        self._hook = hook
        self.follow_symlinks = follow_symlinks
        if not callable(ignore_error):
            _ignore_error = lambda *args, **kwargs: bool(ignore_error)
        else:
            _ignore_error = ignore_error
        self.ignore_error = _ty.cast(_OnPathSyncerError, _ignore_error)

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
    ):
        if not dry_run and do:
            try:
                do()
            except Exception as e:
                if self.ignore_error(e, source, target, event):
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
        ignore_error: (
            bool | _ty.Callable[[Exception, PathAndStat, PathAndStat], None]
        ) = False,
    ):
        checksum = self.checksum

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

        if self.hook(source, target, SyncEvent.SyncStart, False, start):
            return

        if not source.exists():
            if self.remove_missing:
                if self.hook(
                    source,
                    target,
                    SyncEvent.RemovedMissing,
                    dry_run,
                    lambda: target.path.rm(recursive=True, missing_ok=True),
                ):
                    return
        elif source.is_symlink():
            error = NotImplementedError("symlink sync not implemented yet")
            if not ignore_error(error, source, target, None):
                raise error
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

                if self.hook(source, target, SyncEvent.Copy, dry_run, copy):
                    return
        else:
            if target.is_file():
                if self.hook(
                    source,
                    target,
                    SyncEvent.TypeMismatch,
                    dry_run,
                    lambda: target.path.unlink(),
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
                                )

                        self.hook(
                            source,
                            target,
                            SyncEvent.CheckTargetChild,
                            False,
                            checkchild,
                        )

                self.hook(
                    source,
                    target,
                    SyncEvent.CheckTargetChildren,
                    False,
                    checkchildren,
                )

            def sync_children():
                for child in get_source_children():
                    self.hook(
                        source,
                        target,
                        SyncEvent.SyncChild,
                        False,
                        lambda child=child: self.sync(
                            child,
                            target.path / (child.path.name or child.path.parent.name),
                            dry_run,
                        ),
                    )

            self.hook(source, target, SyncEvent.SyncChildren, False, sync_children)

        self.hook(source, target, SyncEvent.Synced, dry_run)
