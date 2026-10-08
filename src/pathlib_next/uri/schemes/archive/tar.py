from __future__ import annotations

import io as _io
import stat as _stat
import tarfile as _tarfile
import zlib as _zlib

try:
    from lzma import LZMAError as _LZMAError
except ImportError:  # pragma: no cover - a Python built without lzma
    _LZMAError = _zlib.error

from ....utils.stat import FileStat
from ._base import (
    ArchiveUri,
    _ArchiveBackend,
    _LazyReadFile,
    _local_outer_path,
    _normalize_member_name,
    _spool,
)


class _TarBackend(_ArchiveBackend):
    """A tar archive, plain or compressed. A local one is read from the file
    as it is needed (a compressed one is decompressed from its start for
    each member it reaches), a non-local one is held in memory whole."""

    __slots__ = ("_members", "_source")

    _FORMAT_ERROR = _tarfile.ReadError

    def __init__(self, outer):
        super().__init__(outer)
        self._members = {}
        self._source = None

    def _is_format_error(self, error):
        # What the decompressors raise over an archive that is cut short or
        # damaged. Their `OSError`s (a bad gzip header or CRC, bzip2's
        # "Invalid data stream") carry no errno, which an I/O failure does.
        return isinstance(
            error, (_tarfile.TarError, EOFError, _zlib.error, _LZMAError)
        ) or (isinstance(error, OSError) and error.errno is None)

    def _open(self):
        local = _local_outer_path(self.outer)
        if local is not None:
            source = _LazyReadFile(local)
        else:
            source = _io.BytesIO(self._outer_bytes())
        try:
            with self._damage_as_format_error():
                handle = _tarfile.open(fileobj=source, mode="r:*")
                members = {}
                for info in handle.getmembers():
                    # A later entry of the same name wins, as in tarfile itself.
                    members[info.name] = info
        except BaseException:
            source.close()
            raise
        self._members = members
        self._source = source
        self._release()
        return handle

    def _release(self):
        # No OS handle is kept between operations: on Windows one blocks
        # deleting or replacing the archive.
        if isinstance(self._source, _LazyReadFile):
            self._source.release()

    def _close_handle(self):
        source, self._source = self._source, None
        super()._close_handle()
        if source is not None:
            source.close()

    def _entries(self):
        return list(self._members.items())

    @staticmethod
    def _is_directory(info):
        return info.isdir()

    @staticmethod
    def _stat_of(info, snapshot):
        size = info.size
        if info.isdir():
            kind = _stat.S_IFDIR
        elif info.isfile() or info.islnk():
            kind = _stat.S_IFREG
            if info.islnk():
                # A hard link carries no data of its own.
                target = _normalize_member_name(info.linkname)
                raw = snapshot.index.get(target) if target else None
                size = snapshot.infos[raw].size if raw is not None else 0
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

    def read_member(self, path):
        with self._lock:
            handle = self.handle
            member = self._members[path]  # raises KeyError if missing
            try:
                with self._damage_as_format_error():
                    f = handle.extractfile(member)
                if f is None:
                    raise IsADirectoryError(path)
                # Copied out under the lock: a live stream shares the
                # handle's file object (and decompressor) with every other
                # reader. A large member goes to a temporary file.
                with self._damage_as_format_error(), f:
                    return _spool(f)
            finally:
                self._release()


class TarUri(ArchiveUri):
    """`tar:` scheme (also handles `.tar.gz`/`.tar.bz2`/`.tar.xz` via
    `tarfile`'s auto-detected "r:*" mode). Read-only. Members stored with a
    `./` prefix are addressed without it. `archive+tar:` is the same scheme
    under a second name."""

    __SCHEMES = ("tar", "archive+tar")
    __slots__ = ()
    _backend_cls = _TarBackend
