import enum as _enum
import typing as _ty

from ..path import Path
from ..utils.stat import FileStat


class SyncEvent(_enum.Enum):
    Copy = 1
    RemovedMissing = 2
    SyncStart = 5
    Synced = 3
    CreatedDirectory = 4


class PathSyncer(object):
    __slots__ = ("checksum", "_hook", "remove_missing", "follow_symlinks")
    EVENT_LOG_FORMAT = "[{event}] Source:{source} Target:{target} DryRun:{dry_run}"

    def __init__(
        self,
        checksum: _ty.Callable[[Path], int],
        /,
        remove_missing: bool = False,
        follow_symlinks: bool = True,
        hook: _ty.Callable[[Path, Path, SyncEvent, bool], None] = None,
    ) -> None:
        self.checksum = checksum
        self.remove_missing = remove_missing
        self._hook = hook
        self.follow_symlinks = follow_symlinks

    def log(self, msg: str, **kwargs: str):
        print(msg.format_map(kwargs))

    def hook(
        self,
        source: Path,
        target: Path,
        event: SyncEvent,
        dry_run: bool,
    ):
        if self._hook:
            self._hook(source, target, event, dry_run)
        self.log(
            self.EVENT_LOG_FORMAT,
            event=event,
            source=source,
            target=target,
            dry_run=dry_run,
        )

    def sync(self, source: Path, target: Path, /, dry_run: bool = False):
        checksum = self.checksum
        self.hook(source, target, SyncEvent.SyncStart, dry_run)

        src_stat = FileStat.from_path(source, follow_symlink=self.follow_symlinks)
        tgt_stat = FileStat.from_path(target, follow_symlink=self.follow_symlinks)

        if src_stat is None:
            if self.remove_missing:
                if not dry_run:
                    target.rm(recursive=True, missing_ok=True)
                self.hook(source, target, SyncEvent.RemovedMissing, dry_run)
        elif src_stat.is_symlink():
            raise NotImplementedError("symlink sync not implemented yet")
        elif src_stat.is_file():
            synced = False
            if tgt_stat and tgt_stat.is_file():
                if checksum(target) == checksum(source):
                    synced = True
            if not synced:
                if not dry_run:
                    if tgt_stat:
                        if tgt_stat.is_file() or tgt_stat.is_symlink():
                            target.unlink()
                        else:
                            if target:
                                target.rm(recursive=tgt_stat.is_dir())
                    source.copy(target)
                self.hook(source, target, SyncEvent.Copy, dry_run)
        else:
            t_exists = tgt_stat != None
            if tgt_stat and tgt_stat.is_file():
                if not dry_run:
                    target.unlink()
                t_exists = False

            if not t_exists:
                if not dry_run:
                    target.mkdir()
                self.hook(source, target, SyncEvent.CreatedDirectory, dry_run)

            if self.remove_missing:
                for child in target.iterdir():
                    if not (source / child.name).exists():
                        if not dry_run:
                            child.rm(recursive=True)
                        self.hook(source, target, SyncEvent.RemovedMissing, dry_run)

            for child in source.iterdir():
                self.sync(child, target / child.name, dry_run)

        self.hook(source, target, SyncEvent.Synced, dry_run)
