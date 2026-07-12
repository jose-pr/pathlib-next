from __future__ import annotations

import io as _io
import os as _os
import tempfile as _tempfile
import threading as _threading
import time as _time
import zipfile as _zipfile

from ....utils.stat import FileStat
from ..file import FileUri
from ._base import ArchiveUri, _ArchiveBackend


class _ZipBackend(_ArchiveBackend):
    __slots__ = ("_lock",)

    def __init__(self, outer):
        super().__init__(outer)
        self._lock = _threading.RLock()

    @property
    def writable(self) -> bool:
        # Writing individual entries in place (via ZipFile.open(name, "w"))
        # only works when we hold the archive open in "a" mode against a
        # real seekable local file -- not for remote/embedded outer URIs.
        return isinstance(self.outer, FileUri)

    def _open(self):
        if self.writable:
            return _zipfile.ZipFile(str(self.outer.filepath), mode="a")
        return _zipfile.ZipFile(_io.BytesIO(self.outer.read_bytes()), mode="r")

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
            if path in self.handle.namelist():
                # zipfile has no in-place entry update -- writestr()-ing an
                # existing name just appends a duplicate. Overwriting an
                # existing entry needs a full-archive rewrite.
                self._rewrite(overwrite={path: data})
                return
            # zipfile only persists the central directory to disk when the
            # *archive* (not just the entry) is closed -- close and drop the
            # cached handle so both this backend's next operation and any
            # other reference to this shared backend see the write.
            self.handle.writestr(path, data)
            self.handle.close()
            self._handle = None

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
        `overwrite`. Writes to a temp file next to the outer archive file
        and atomically replaces it (`os.replace`) so a crash mid-rewrite
        can't leave a corrupt archive. Must be called with `self._lock`
        held (all public mutators above already do)."""
        rename = dict(rename or {})
        overwrite = dict(overwrite or {})
        prefix_renames = {old: new for old, new in rename.items() if old.endswith("/")}

        def _remap(name: str):
            if name in exclude:
                return None
            if name in rename:
                return rename[name]
            for old_prefix, new_prefix in prefix_renames.items():
                if name.startswith(old_prefix):
                    return new_prefix + name[len(old_prefix) :]
            return name

        outer_path = self.outer.filepath
        fd, tmp_name = _tempfile.mkstemp(
            dir=str(outer_path.parent), prefix=".pathlib_next-zip-", suffix=".tmp"
        )
        _os.close(fd)
        try:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            with _zipfile.ZipFile(str(outer_path), "r") as src, _zipfile.ZipFile(
                tmp_name, "w", _zipfile.ZIP_DEFLATED
            ) as dst:
                written = set()
                for info in src.infolist():
                    new_name = _remap(info.filename)
                    if new_name is None:
                        continue
                    data = overwrite.pop(new_name, None)
                    if data is None:
                        data = overwrite.pop(info.filename, None)
                    dst.writestr(new_name, data if data is not None else src.read(info.filename))
                    written.add(new_name)
                for name, data in overwrite.items():
                    if name not in written:
                        dst.writestr(name, data)
            _os.replace(tmp_name, str(outer_path))
        except BaseException:
            try:
                _os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def member_stat(self, path):
        with self._lock:
            info = self.handle.getinfo(path)
            mtime = int(_time.mktime((*info.date_time, 0, 0, -1))) if info.date_time else 0
            return FileStat(st_size=info.file_size, st_mtime=mtime, is_dir=path.endswith("/"))


class ZipUri(ArchiveUri):
    """`zip:` scheme. Read/write: write support (new entries, overwriting
    existing entries, `unlink`/`rmdir`/`rename`) works when the outer
    archive is a local `file:` URI; every other outer scheme is read-only
    (fetched fully into memory first). New entries are appended in place
    (cheap); overwriting/deleting/renaming an existing entry requires a
    full-archive rewrite (`_ZipBackend._rewrite`) since `zipfile` has no
    in-place entry mutation -- each such call rewrites the whole archive to
    a temp file and atomically replaces the original (`os.replace`). Write
    methods (`_open` write modes, `_mkdir`, `unlink`, `rmdir`, `rename`)
    live on the shared `ArchiveUri` base -- they're generic, gated on
    `self.backend.writable`, which only this backend ever reports `True`."""

    __SCHEMES = ("zip",)
    __slots__ = ()
    _backend_cls = _ZipBackend
