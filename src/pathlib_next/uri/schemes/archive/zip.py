from __future__ import annotations

import io as _io
import os as _os
import shutil as _shutil
import stat as _stat
import struct as _struct
import tempfile as _tempfile
import time as _time
import zipfile as _zipfile
from contextlib import contextmanager as _contextmanager

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


def _member_mode(info: _zipfile.ZipInfo) -> "int | None":
    """The Unix mode stored for `info`, or None when there is none: only a
    Unix-made entry (`create_system == 3`) carries one, in the high 16 bits
    of `external_attr`. A symlink or other special type is not modelled by
    the archive schemes and reports no mode either."""
    mode = info.external_attr >> 16 if info.create_system == 3 else 0
    if not _stat.S_IMODE(mode):
        return None
    kind = _stat.S_IFMT(mode)
    if info.filename.endswith("/"):
        kind = _stat.S_IFDIR
    elif kind == 0:
        kind = _stat.S_IFREG
    if kind not in (_stat.S_IFREG, _stat.S_IFDIR):
        return None
    return kind | _stat.S_IMODE(mode)


class _LazyReadFile:
    """A read-only, seekable view of a local file that holds an OS handle
    only while an operation runs: `release()` closes it, and the next
    read/seek reopens it at the same position. A shared `ZipFile` over one
    therefore never keeps the archive open between operations (on Windows
    an open handle blocks deleting or replacing the file)."""

    def __init__(self, path: str):
        self.name = path
        self._pos = 0
        # Opened eagerly: a missing file raises FileNotFoundError here,
        # which `zipfile` would otherwise turn into BadZipFile.
        self._fp = open(path, "rb")

    def _file(self):
        if self._fp is None:
            self._fp = open(self.name, "rb")
            self._fp.seek(self._pos)
        return self._fp

    def read(self, size=-1):
        data = self._file().read(size)
        self._pos = self._fp.tell()
        return data

    def seek(self, offset, whence=0):
        self._pos = self._file().seek(offset, whence)
        return self._pos

    def tell(self):
        return self._pos

    def seekable(self):
        return True

    def release(self):
        fp, self._fp = self._fp, None
        if fp is not None:
            fp.close()

    close = release


class _ZipBackend(_ArchiveBackend):
    __slots__ = ()

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
            fp = _LazyReadFile(str(self.outer.filepath))
            try:
                return _zipfile.ZipFile(fp, mode="r")
            finally:
                fp.release()
        return _zipfile.ZipFile(_io.BytesIO(self.outer.read_bytes()), mode="r")

    def _close_handle(self):
        # `ZipFile.close()` leaves a passed-in file object open.
        fp = getattr(self._handle, "fp", None)
        super()._close_handle()
        if isinstance(fp, _LazyReadFile):
            fp.release()

    @_contextmanager
    def _reading(self):
        """The shared handle, under the lock; the OS file handle behind it
        is released again when the operation ends."""
        with self._lock:
            handle = self.handle
            try:
                yield handle
            finally:
                if isinstance(handle.fp, _LazyReadFile):
                    handle.fp.release()

    def names(self):
        with self._reading() as handle:
            return handle.namelist()

    def read_member(self, path):
        with self._reading() as handle:
            # Read fully into memory rather than returning the live
            # ZipExtFile: callers may hold the returned stream open across
            # further mutations (unlink/rename/write) on this same shared
            # backend, and those close+reopen the underlying handle.
            return _io.BytesIO(handle.read(path))  # raises KeyError if missing

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
            if path in self.names():
                # zipfile has no in-place entry update -- writestr()-ing an
                # existing name just appends a duplicate. Overwriting an
                # existing entry needs a full-archive rewrite.
                self._rewrite(overwrite={path: data})
                return
            self._append(path, data)

    def _append(self, path: str, data: bytes):
        """Add a new entry without risking the archive: an in-place "a"
        append overwrites the central directory with the new entry's data
        and writes a new one only on close, so dying in between left no
        readable member. The archive is byte-copied (nothing recompressed)
        to a temp file, appended to there, and swapped in by
        `_replace_outer`."""
        outer_path = _os.path.realpath(str(self.outer.filepath))
        if not _zipfile.is_zipfile(outer_path):
            raise _zipfile.BadZipFile(f"File is not a zip file: {outer_path!r}")

        def fill(tmp):
            with open(outer_path, "rb") as original:
                _shutil.copyfileobj(original, tmp)
            # zipfile only persists the central directory to disk when the
            # *archive* (not just the entry) is closed.
            with _zipfile.ZipFile(tmp, "a") as archive:
                archive.writestr(path, data)

        self._replace_outer(fill)

    def _replace_outer(self, fill):
        """Build the new archive with `fill(tmp)` in a temp file next to the
        archive -- resolving a symlink, so the link's target is what gets
        replaced -- fsync it, keep the original's file mode, and atomically
        replace the original (`os.replace`): a failure or crash at any point
        leaves the original untouched and no temp file behind. Must be
        called with `self._lock` held."""
        outer_path = _os.path.realpath(str(self.outer.filepath))
        self._close_handle()
        fd, tmp_name = _tempfile.mkstemp(
            dir=_os.path.dirname(outer_path), prefix=".pathlib_next-zip-", suffix=".tmp"
        )
        try:
            with _os.fdopen(fd, "w+b") as tmp:
                fill(tmp)
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

    def delete_member(self, name: str):
        with self._lock:
            self._rewrite(exclude={name})

    def rename_member(self, old: str, new: str, *, members=None):
        """Rename `old` to `new`. `members` is an explicit raw-name mapping
        for a directory rename, whose members may be spelled several ways
        ("./dir/x") and so cannot be found by prefix on one name."""
        with self._lock:
            self._rewrite(rename=dict(members) if members else {old: new})

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
        zipapp shebang, a self-extractor stub) and its file mode. Written
        through `_replace_outer`, so a crash mid-rewrite can't leave a
        corrupt archive. Must be called with `self._lock` held (all public
        mutators above already do)."""
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

        def fill(tmp):
            with _zipfile.ZipFile(outer_path, "r") as src:
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

        self._replace_outer(fill)

    def member_stat(self, path):
        with self._reading() as handle:
            info = handle.getinfo(path)
            mtime = (
                int(_time.mktime((*info.date_time, 0, 0, -1))) if info.date_time else 0
            )
            return FileStat(
                st_mode=_member_mode(info),
                st_size=info.file_size,
                st_mtime=mtime,
                is_dir=path.endswith("/"),
            )


class ZipUri(ArchiveUri):
    """`zip:` scheme. Read/write: write support (new entries, overwriting
    existing entries, `unlink`/`rmdir`/`rename`) works when the outer
    archive is a local `file:` URI; every other outer scheme is read-only
    (fetched fully into memory first). Every mutation replaces the archive
    atomically (temp file + `os.replace`): a new entry is appended to a
    byte copy of the archive (nothing recompressed); overwriting/deleting/
    renaming an existing entry rewrites the whole archive
    (`_ZipBackend._rewrite`), since `zipfile` has no in-place entry
    mutation, keeping every other entry's metadata. Write methods (`_open` write
    modes, `_mkdir`, `unlink`, `rmdir`, `rename`) live on the shared
    `ArchiveUri` base -- they're generic, gated on `self.backend.writable`,
    which only this backend ever reports `True`."""

    __SCHEMES = ("zip",)
    __slots__ = ()
    _backend_cls = _ZipBackend
