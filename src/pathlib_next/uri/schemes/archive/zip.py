from __future__ import annotations

import io as _io
import os as _os
import shutil as _shutil
import stat as _stat
import struct as _struct
import tempfile as _tempfile
import threading as _threading
import time as _time
import zipfile as _zipfile

from ....utils.stat import FileStat
from ..file import FileUri
from ._base import ArchiveUri, _ArchiveBackend

_ZIP64_EXTRA_ID = 0x0001


def _strip_zip64_extra(extra: bytes) -> bytes:
    """Drop zip64 extended-information records from a raw extra field.
    `zipfile` writes its own when an entry needs them; a copied one would
    carry the old sizes/offsets and conflict with the rewritten entry."""
    kept = bytearray()
    offset = 0
    while offset + 4 <= len(extra):
        header_id, size = _struct.unpack("<HH", extra[offset : offset + 4])
        end = offset + 4 + size
        if header_id != _ZIP64_EXTRA_ID:
            kept += extra[offset:end]
        offset = end
    return bytes(kept)


def _copy_zipinfo(info: _zipfile.ZipInfo, name: str) -> _zipfile.ZipInfo:
    """A fresh `ZipInfo` for `name` carrying `info`'s member metadata
    (timestamp, compression, permissions, comment, extra) -- the fields
    `writestr()` would otherwise reset. Sizes, CRC and offsets are left for
    `writestr()` to compute."""
    copied = _zipfile.ZipInfo(name, date_time=info.date_time)
    copied.compress_type = info.compress_type
    copied.comment = info.comment
    copied.extra = _strip_zip64_extra(info.extra)
    copied.create_system = info.create_system
    copied.internal_attr = info.internal_attr
    copied.external_attr = info.external_attr
    return copied


class _ZipBackend(_ArchiveBackend):
    __slots__ = ("_lock",)

    def __init__(self, outer):
        super().__init__(outer)
        self._lock = _threading.RLock()

    @property
    def writable(self) -> bool:
        # Writing entries needs a real seekable local file -- not a
        # remote/embedded outer URI. The shared read handle is still opened
        # "r"; writes open their own handle (`write_member`/`_rewrite`).
        return isinstance(self.outer, FileUri)

    def _open(self):
        if self.writable:
            # Never "a" for reads: append mode opens the file r+b (fails on a
            # read-only archive), creates a missing file, and on close appends
            # an end-of-central-directory record to a file that is not a zip.
            return _zipfile.ZipFile(str(self.outer.filepath), mode="r")
        return _zipfile.ZipFile(_io.BytesIO(self.outer.read_bytes()), mode="r")

    def _close_handle(self):
        # Drop the cached read handle so the next operation (through any
        # instance sharing this backend) reopens and sees the write.
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def names(self):
        with self._lock:
            return self.handle.namelist()

    def read_member(self, path):
        with self._lock:
            # Read fully into memory rather than returning the live
            # ZipExtFile: callers may hold the returned stream open across
            # further mutations (unlink/rename/write) on this same shared
            # backend, and those close+reopen the underlying handle.
            return _io.BytesIO(self.handle.read(path))  # raises KeyError if missing

    def write_member(self, path: str, data: bytes):
        with self._lock:
            outer_path = str(self.outer.filepath)
            if not _os.path.lexists(outer_path):
                # First write into a missing archive creates it ("x": never
                # clobber a file that appeared in the meantime).
                self._close_handle()
                with _zipfile.ZipFile(outer_path, "x") as archive:
                    archive.writestr(path, data)
                return
            if path in self.handle.namelist():
                # zipfile has no in-place entry update -- writestr()-ing an
                # existing name just appends a duplicate. Overwriting an
                # existing entry needs a full-archive rewrite.
                self._rewrite(overwrite={path: data})
                return
            self._close_handle()
            if not _zipfile.is_zipfile(outer_path):
                raise _zipfile.BadZipFile(f"File is not a zip file: {outer_path!r}")
            # zipfile only persists the central directory to disk when the
            # *archive* (not just the entry) is closed.
            with _zipfile.ZipFile(outer_path, "a") as archive:
                archive.writestr(path, data)

    def delete_member(self, name: str):
        with self._lock:
            self._rewrite(exclude={name})

    def rename_member(self, old: str, new: str):
        with self._lock:
            self._rewrite(rename={old: new})

    def _rewrite(
        self,
        *,
        exclude: "set[str]" = frozenset(),
        rename: "dict[str, str] | None" = None,
        overwrite: "dict[str, bytes] | None" = None,
    ):
        """Safely rewrite the whole archive: drop names in `exclude`,
        rename per `rename` (old->new; a `<name>/` key also renames every
        entry nested under that prefix), and set/add content for names in
        `overwrite`. A renamed entry replaces any existing entry at its new
        name (POSIX rename); otherwise duplicate names collapse to the last
        one, which is the entry `zipfile` itself reads.

        Every kept entry keeps its metadata (`_copy_zipinfo`), and the
        archive keeps its comment, any bytes before the first member (a
        zipapp shebang, a self-extractor stub) and its file mode. Writes to a
        temp file next to the archive -- resolving a symlink, so the link's
        target is what gets replaced -- fsyncs it and atomically replaces the
        original (`os.replace`) so a crash mid-rewrite can't leave a corrupt
        archive. Must be called with `self._lock` held (all public mutators
        above already do)."""
        rename = dict(rename or {})
        overwrite = dict(overwrite or {})
        prefix_renames = {old: new for old, new in rename.items() if old.endswith("/")}

        def _remap(name: str) -> "tuple[str | None, bool]":
            if name in exclude:
                return None, False
            if name in rename:
                return rename[name], True
            for old_prefix, new_prefix in prefix_renames.items():
                if name.startswith(old_prefix):
                    return new_prefix + name[len(old_prefix) :], True
            return name, False

        outer_path = _os.path.realpath(str(self.outer.filepath))
        self._close_handle()
        fd, tmp_name = _tempfile.mkstemp(
            dir=_os.path.dirname(outer_path), prefix=".pathlib_next-zip-", suffix=".tmp"
        )
        try:
            with _os.fdopen(fd, "wb") as tmp, _zipfile.ZipFile(outer_path, "r") as src:
                infos = src.infolist()
                plan = []
                winner = {}
                for info in infos:
                    new_name, renamed = _remap(info.filename)
                    if new_name is None:
                        continue
                    previous = winner.get(new_name)
                    if previous is None or renamed or not plan[previous][2]:
                        winner[new_name] = len(plan)
                    plan.append((new_name, info, renamed))

                prefix_end = min(
                    [info.header_offset for info in infos] + [src.start_dir]
                )
                with open(outer_path, "rb") as original:
                    tmp.write(original.read(prefix_end))

                with _zipfile.ZipFile(tmp, "w", _zipfile.ZIP_DEFLATED) as dst:
                    dst.comment = src.comment
                    written = set()
                    for index, (new_name, info, _renamed) in enumerate(plan):
                        if winner[new_name] != index:
                            continue
                        zinfo = _copy_zipinfo(info, new_name)
                        data = overwrite.pop(new_name, None)
                        if data is None:
                            data = overwrite.pop(info.filename, None)
                        if data is None:
                            data = src.read(info)
                        else:
                            zinfo.date_time = _time.localtime()[:6]
                        dst.writestr(zinfo, data)
                        written.add(new_name)
                    for name, data in overwrite.items():
                        if name not in written:
                            dst.writestr(name, data)
                tmp.flush()
                _os.fsync(tmp.fileno())
            _shutil.copymode(outer_path, tmp_name)
            _os.replace(tmp_name, outer_path)
        except BaseException:
            try:
                _os.chmod(tmp_name, _stat.S_IREAD | _stat.S_IWRITE)
                _os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def member_stat(self, path):
        with self._lock:
            info = self.handle.getinfo(path)
            mtime = (
                int(_time.mktime((*info.date_time, 0, 0, -1))) if info.date_time else 0
            )
            return FileStat(
                st_size=info.file_size, st_mtime=mtime, is_dir=path.endswith("/")
            )


class ZipUri(ArchiveUri):
    """`zip:` scheme. Read/write: write support (new entries, overwriting
    existing entries, `unlink`/`rmdir`/`rename`) works when the outer
    archive is a local `file:` URI; every other outer scheme is read-only
    (fetched fully into memory first). New entries are appended in place
    (cheap); overwriting/deleting/renaming an existing entry requires a
    full-archive rewrite (`_ZipBackend._rewrite`) since `zipfile` has no
    in-place entry mutation -- each such call rewrites the whole archive to
    a temp file and atomically replaces the original (`os.replace`),
    keeping every other entry's metadata. Write methods (`_open` write
    modes, `_mkdir`, `unlink`, `rmdir`, `rename`) live on the shared
    `ArchiveUri` base -- they're generic, gated on `self.backend.writable`,
    which only this backend ever reports `True`."""

    __SCHEMES = ("zip",)
    __slots__ = ()
    _backend_cls = _ZipBackend
