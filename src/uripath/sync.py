import typing as _ty
from .uri import Uri
import enum as _enum


class SyncEvent(_enum.Enum):
    Copy = 1
    RemovedMissing = 2
    SyncStart = 5
    Synced = 3
    CreatedDirectory = 4


class UriSyncer(object):
    __slots__ = ("checksum", "_hook", "remove_missing")
    EVENT_LOG_FORMAT = "[{event}] Source:{source} Target:{target} DryRun:{dry_run}"

    def __init__(
        self,
        checksum: _ty.Callable[[Uri], int],
        /,
        remove_missing: bool = False,
        hook: _ty.Callable[[Uri, Uri, SyncEvent, bool], None] = None,
    ) -> None:
        self.checksum = checksum
        self.remove_missing = remove_missing
        self._hook = hook

    def log(self, msg: str, **kwargs: str):
        print(msg.format_map(kwargs))

    def hook(self, source: Uri, target: Uri, event: SyncEvent, dry_run: bool):
        if self._hook:
            self._hook(source, target, event, dry_run)
        self.log(
            self.EVENT_LOG_FORMAT,
            event=event,
            source=source,
            target=target,
            dry_run=dry_run,
        )

    def sync(self, source: Uri, target: Uri, /, dry_run: bool = False):
        source, target = source._src_dest(target)
        checksum = self.checksum
        self.hook(source, target, SyncEvent.SyncStart, dry_run)

        if not source.exists():
            if self.remove_missing:
                if not dry_run:
                    target.rm(recursive=True, missing_ok=True)
                self.hook(source, target, SyncEvent.RemovedMissing, dry_run)

        elif source.is_file():
            synced = False
            if target.is_file():
                if checksum(target) == checksum(source):
                    synced = True
            if not synced:
                if not dry_run:
                    target.rm(recursive=True, missing_ok=True)
                    source.copy(target)
                self.hook(source, target, SyncEvent.Copy, dry_run)
        else:
            if target.is_file():
                if not dry_run:
                    target.unlink()

            if not target.exists():
                if not dry_run:
                    target.mkdir()
                self.hook(source, target, SyncEvent.CreatedDirectory, dry_run)
            childs = []
            for child in source.iterdir():
                self.sync(child, target / child.name, dry_run)
                childs.append(child.name)
            if self.remove_missing:
                for child in target.iterdir():
                    if child.name not in childs:
                        if not dry_run:
                            child.rm(recursive=True)
                        self.hook(source, target, SyncEvent.RemovedMissing, dry_run)

        self.hook(source, target, SyncEvent.Synced, dry_run)
