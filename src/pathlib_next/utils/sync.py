import enum as _enum
import typing as _ty

from ..path import Path
from ..utils.stat import FileStat


class SyncEvent(_enum.Enum):
    Copy = 1
    RemovedMissing = 2
    TypeMismatch = 6
    SyncStart = 5
    Synced = 3
    CreatedDirectory = 4
    CheckTargetChild = _enum.auto()
    CheckTargetChildren = _enum.auto()
    SyncChild = _enum.auto()
    SyncChildren = _enum.auto()


class PathAndStat(object):
    __slots__ = ("_path", "_stat")

    def __init__(self, path: Path, *, follow_symlink=None) -> None:
        self._path = path
        self.refresh(follow_symlink)

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
    __slots__ = (
        "checksum",
        "_hook",
        "remove_missing",
        "follow_symlinks",
        "ignore_error",
    )
    EVENT_LOG_FORMAT = "[{event}] Source:{source} Target:{target} DryRun:{dry_run}"

    def __init__(
        self,
        checksum: _ty.Callable[[PathAndStat], int],
        /,
        remove_missing: bool = False,
        follow_symlinks: bool = True,
        hook: _ty.Callable[[PathAndStat, PathAndStat, SyncEvent, bool], None] = None,
        ignore_error: _OnPathSyncerError | bool = False,
    ) -> None:
        self.checksum = checksum
        self.remove_missing = remove_missing
        self._hook = hook
        self.follow_symlinks = follow_symlinks
        if not callable(ignore_error):
            _ignore_error = lambda *args, **kwargs: bool(ignore_error)
        else:
            _ignore_error = ignore_error
        self.ignore_error = _ty.cast(_OnPathSyncerError, _ignore_error)

    def log(self, msg: str, **kwargs: str):
        print(msg.format_map(kwargs))

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
        self.log(
            self.EVENT_LOG_FORMAT,
            event=event,
            source=source,
            target=target,
            dry_run=dry_run,
        )

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

            if self.remove_missing:

                def checkchildren():
                    for child in target.path.iterdir():

                        def checkchild():
                            if not (source.path / child.name).exists():
                                self.hook(
                                    source,
                                    target,
                                    SyncEvent.RemovedMissing,
                                    dry_run,
                                    lambda: child.rm(recursive=True),
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
                for child in source.path.iterdir():
                    self.hook(
                        source,
                        target,
                        SyncEvent.SyncChild,
                        False,
                        lambda: self.sync(
                            child,
                            target.path / (child.name or child.parent.name),
                            dry_run,
                        ),
                    )

            self.hook(source, target, SyncEvent.SyncChildren, False, sync_children)

        self.hook(source, target, SyncEvent.Synced, dry_run)
