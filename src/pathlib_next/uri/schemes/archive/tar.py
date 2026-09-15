from __future__ import annotations

import io as _io
import stat as _stat
import tarfile as _tarfile

from ....utils.stat import FileStat
from ._base import ArchiveUri, _ArchiveBackend


def _member_name(name: str) -> str:
    """The name a member is addressed by: without the `./` prefix that
    `tar -C dir .`, `TarFile.add(dir, arcname=".")` and
    `shutil.make_archive(..., root_dir=dir)` put on every member. A `.`
    entry (the archive root itself) becomes ""."""
    while name.startswith("./"):
        name = name[2:].lstrip("/")
    return "" if name == "." else name


class _TarBackend(_ArchiveBackend):
    __slots__ = ("_members",)

    def __init__(self, outer):
        super().__init__(outer)
        self._members = {}

    def _open(self):
        handle = _tarfile.open(fileobj=_io.BytesIO(self.outer.read_bytes()), mode="r:*")
        members = {}
        for info in handle.getmembers():
            name = _member_name(info.name)
            if name:
                # A later entry of the same name wins, as in tarfile itself.
                members[name] = info
        self._members = members
        return handle

    def names(self):
        with self._lock:
            self.handle  # (re)opens and rebuilds the index when needed
            return list(self._members)

    def read_member(self, path):
        with self._lock:
            handle = self.handle
            member = self._members[path]  # raises KeyError if missing
            f = handle.extractfile(member)
            if f is None:
                raise IsADirectoryError(path)
            # Fully read under the lock: a live stream shares the handle's
            # file object (and decompressor) with every other reader.
            with f:
                return _io.BytesIO(f.read())

    def member_stat(self, path):
        with self._lock:
            self.handle
            members = self._members
            info = members[path]
            size = info.size
            if info.isdir():
                kind = _stat.S_IFDIR
            elif info.isfile() or info.islnk():
                kind = _stat.S_IFREG
                if info.islnk():
                    # A hard link carries no data of its own.
                    target = members.get(_member_name(info.linkname))
                    size = target.size if target is not None else 0
            else:
                # Symlinks and special files are not modelled: no mode.
                kind = 0
            perms = _stat.S_IMODE(info.mode)
            return FileStat(
                st_mode=kind | perms if kind and perms else None,
                st_size=size,
                st_mtime=int(info.mtime),
                is_dir=info.isdir(),
            )


class TarUri(ArchiveUri):
    """`tar:` scheme (also handles `.tar.gz`/`.tar.bz2`/`.tar.xz` via
    `tarfile`'s auto-detected "r:*" mode). Read-only. Members stored with a
    `./` prefix are addressed without it."""

    __SCHEMES = ("tar",)
    __slots__ = ()
    _backend_cls = _TarBackend
