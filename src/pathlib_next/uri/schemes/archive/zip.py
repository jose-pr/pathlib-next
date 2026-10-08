from __future__ import annotations

import errno as _errno
import io as _io
import os as _os
import shutil as _shutil
import stat as _stat
import struct as _struct
import tempfile as _tempfile
import time as _time
import zipfile as _zipfile
import zlib as _zlib
from contextlib import contextmanager as _contextmanager

from ....utils.stat import FileStat
from ..file import FileUri
from ._base import ArchiveUri, _ArchiveBackend, _LazyReadFile, _spool

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


_COPY_CHUNK = 1024 * 1024
_LOCAL_HEADER = _struct.Struct("<4s2B4HL2L2H")


def _copy_range(source, target, length: int) -> None:
    """Copy `length` bytes from `source` to `target` in chunks."""
    while length > 0:
        chunk = source.read(min(length, _COPY_CHUNK))
        if not chunk:
            raise _zipfile.BadZipFile("archive ended inside an entry")
        target.write(chunk)
        length -= len(chunk)


def _copy_entry(original, dst, info, new_name: str, end: "int | None") -> bool:
    """Append the entry `info` to the archive being written by `dst` under
    `new_name`, copying its compressed bytes as they are: the local header, the
    data and any data descriptor lie between `info.header_offset` and `end`
    (where the next record starts). Only a renamed entry, or one whose name
    the central directory would write in another encoding than the local
    header, gets a new local header. False, with nothing written, when the
    record is not laid out as its directory says; the caller then recompresses
    it."""
    start = info.header_offset
    if end is None or start < 0 or end <= start:
        return False
    original.seek(start)
    header = original.read(_LOCAL_HEADER.size)
    if len(header) < _LOCAL_HEADER.size:
        return False
    fields = _LOCAL_HEADER.unpack(header)
    header_size = _LOCAL_HEADER.size + fields[10] + fields[11]
    if fields[0] != b"PK\x03\x04" or start + header_size + info.compress_size > end:
        return False
    verbatim = new_name == info.filename and (
        info.filename.isascii() or info.flag_bits & 0x800
    )
    if not verbatim and (
        info.flag_bits & 0x08
        and max(info.file_size, info.compress_size) > _zipfile.ZIP64_LIMIT
    ):
        return False
    copied = _copy_zipinfo(info, new_name)
    for name in (
        "create_version",
        "extract_version",
        "reserved",
        "flag_bits",
        "volume",
        "CRC",
        "compress_size",
        "file_size",
    ):
        setattr(copied, name, getattr(info, name))
    out = dst.fp
    out.seek(dst.start_dir)
    copied.header_offset = out.tell()
    if verbatim:
        original.seek(start)
        _copy_range(original, out, end - start)
    else:
        out.write(copied.FileHeader())
        original.seek(start + header_size)
        _copy_range(original, out, end - start - header_size)
    dst.start_dir = out.tell()
    dst.filelist.append(copied)
    dst.NameToInfo[copied.filename] = copied
    return True


class _ZipBackend(_ArchiveBackend):
    __slots__ = ()

    _FORMAT_ERROR = _zipfile.BadZipFile

    def _is_format_error(self, error):
        # `zipfile` lets the decoders' own errors through for a damaged
        # archive: an inflate failure, a short header, a truncated stream, a
        # name that does not decode, an offset that cannot be seeked to, and a
        # version it does not know (`NotImplementedError` while reading the
        # central directory).
        return isinstance(
            error,
            (_zlib.error, _struct.error, EOFError, UnicodeDecodeError, OverflowError),
        ) or (isinstance(error, OSError) and error.errno == _errno.EINVAL)

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
                return self._read_directory(fp)
            finally:
                fp.release()
        return self._read_directory(_io.BytesIO(self._outer_bytes()))

    def _read_directory(self, fileobj):
        try:
            with self._damage_as_format_error():
                return _zipfile.ZipFile(fileobj, mode="r")
        except NotImplementedError as error:
            # An extract version the central directory declares and
            # `zipfile` does not know: the directory is not one.
            raise _zipfile.BadZipFile(str(error)) from error

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
                with self._damage_as_format_error():
                    yield handle
            finally:
                if isinstance(handle.fp, _LazyReadFile):
                    handle.fp.release()

    def _entries(self):
        return [(info.filename, info) for info in self._handle.infolist()]

    @staticmethod
    def _is_directory(info):
        return info.filename.endswith("/")

    @staticmethod
    def _stat_of(info, snapshot):
        mtime = int(_time.mktime((*info.date_time, 0, 0, -1))) if info.date_time else 0
        return FileStat(
            st_mode=_member_mode(info),
            st_size=info.file_size,
            st_mtime=mtime,
            is_dir=info.filename.endswith("/"),
        )

    def read_member(self, path):
        with self._reading() as handle:
            # Copied out rather than returned as the live ZipExtFile: callers
            # may hold the returned stream open across further mutations
            # (unlink/rename/write) on this same shared backend, and those
            # close+reopen the underlying handle. A large member goes to a
            # temporary file, not to memory.
            with handle.open(path) as source:  # raises KeyError if missing
                return _spool(source)

    def write_member(self, path: str, data: bytes):
        with self._lock:
            outer_path = str(self.outer.filepath)
            if not _os.path.lexists(outer_path):
                # First write into a missing archive creates it ("x": never
                # clobber a file that appeared in the meantime).
                self._close_handle()
                with _zipfile.ZipFile(
                    outer_path, "x", compression=_zipfile.ZIP_DEFLATED
                ) as archive:
                    archive.writestr(path, data)
                return
            if path in self.snapshot().infos:
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
            with _zipfile.ZipFile(
                tmp, "a", compression=_zipfile.ZIP_DEFLATED
            ) as archive:
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
        if not _os.access(outer_path, _os.W_OK):
            # Replacing swaps the file for a new one and so ignores its mode:
            # a read-only archive is refused here, as an `open(..., "ab")`
            # would refuse it.
            raise PermissionError(
                _errno.EACCES, _os.strerror(_errno.EACCES), outer_path
            )
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

    def delete_members(self, names, *, keep_dir: "str | None" = None):
        """Drop every entry named in `names` in one rewrite. `keep_dir`
        (`"a/b/"`) is written in the same rewrite as a directory entry: a
        directory that exists only through its members must outlive them."""
        with self._lock:
            self._rewrite(
                exclude=set(names),
                overwrite={keep_dir: b""} if keep_dir else None,
            )

    def rename_member(
        self,
        old: str,
        new: str,
        *,
        members=None,
        replace=(),
        keep_dir: "str | None" = None,
    ):
        """Rename `old` to `new`. `members` is an explicit raw-name mapping
        for a directory rename, whose members may be spelled several ways
        ("./dir/x") and so cannot be found by prefix on one name. `replace`
        names entries the rename overwrites, and `keep_dir` is a directory
        entry written in the same rewrite (see `delete_members`)."""
        with self._lock:
            self._rewrite(
                exclude=set(replace),
                rename=dict(members) if members else {old: new},
                overwrite={keep_dir: b""} if keep_dir else None,
            )

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

        An entry that is only kept or renamed is copied as it lies in the
        archive (`_copy_entry`): nothing is decompressed, so the cost is the
        stored size, and an encrypted entry or one with a compression method
        `zipfile` lacks survives. Every kept entry keeps its metadata, and the
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
            with open(outer_path, "rb") as original:
                with _zipfile.ZipFile(original, "r") as src:
                    self._fill(tmp, original, src, _remap, overwrite)

        self._replace_outer(fill)

    @staticmethod
    def _fill(tmp, original, src, remap, overwrite):
        infos = src.infolist()
        plan = []
        winner = {}
        for info in infos:
            new_name, renamed = remap(info.filename)
            if new_name is None:
                continue
            previous = winner.get(new_name)
            if previous is None or renamed or not plan[previous][2]:
                winner[new_name] = len(plan)
            plan.append((new_name, info, renamed))

        # Where each entry's record ends: where the next one starts, or the
        # central directory.
        starts = sorted({info.header_offset for info in infos} | {src.start_dir})
        ends = dict(zip(starts, starts[1:]))
        original.seek(0)
        _copy_range(original, tmp, starts[0])

        with _zipfile.ZipFile(tmp, "w", _zipfile.ZIP_DEFLATED) as dst:
            dst.comment = src.comment
            written = set()
            for index, (new_name, info, _renamed) in enumerate(plan):
                if winner[new_name] != index:
                    continue
                data = overwrite.pop(new_name, None)
                if data is None:
                    data = overwrite.pop(info.filename, None)
                if data is not None:
                    zinfo = _copy_zipinfo(info, new_name)
                    zinfo.date_time = _time.localtime()[:6]
                    dst.writestr(zinfo, data)
                elif not _copy_entry(
                    original, dst, info, new_name, ends.get(info.header_offset)
                ):
                    dst.writestr(_copy_zipinfo(info, new_name), src.read(info))
                written.add(new_name)
            for name, data in overwrite.items():
                if name not in written:
                    dst.writestr(name, data)


class ZipUri(ArchiveUri):
    """`zip:` scheme (`archive+zip:` is a second name for it). Read/write: write support (new entries, overwriting
    existing entries, `unlink`/`rmdir`/`rename`) works when the outer
    archive is a local `file:` URI; every other outer scheme is read-only
    (fetched fully into memory first). Every mutation replaces the archive
    atomically (temp file + `os.replace`), so the result is a new file: other
    hard links keep the old content, and an archive the caller may not write
    is refused first. A new entry is appended, deflated, to a byte copy of the
    archive (nothing recompressed); overwriting/deleting/renaming an existing
    entry rewrites the whole archive (`_ZipBackend._rewrite`), since `zipfile`
    has no in-place entry mutation, keeping every other entry's metadata and
    compression method. Write methods (`_open` write
    modes, `_mkdir`, `unlink`, `rmdir`, `rename`) live on the shared
    `ArchiveUri` base -- they're generic, gated on `self.backend.writable`,
    which only this backend ever reports `True`."""

    __SCHEMES = ("zip", "archive+zip")
    __slots__ = ()
    _backend_cls = _ZipBackend
