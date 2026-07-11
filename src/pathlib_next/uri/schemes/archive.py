from __future__ import annotations

import io as _io
import os as _os
import re as _re
import tarfile as _tarfile
import tempfile as _tempfile
import threading as _threading
import time as _time
import weakref as _weakref
import zipfile as _zipfile

from ...utils.stat import FileStat
from .. import Uri, UriPath
from .file import FileUri

_SEP = "!/"
_SCHEME_RE = _re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _split_archive_path(path: str) -> "tuple[str, str]":
    """Split `<archive-uri>!/<inner-path>` (Java-style separator, as used
    for JAR URLs / NIO ZipFileSystem) into its two halves. No separator (or
    a bare trailing "!") means the archive root."""
    if _SEP in path:
        archive, inner = path.split(_SEP, 1)
    elif path.endswith("!"):
        archive, inner = path[:-1], ""
    else:
        archive, inner = path, ""
    return archive, inner


def _open_outer(archive_uri: str) -> "UriPath":
    if not _SCHEME_RE.match(archive_uri):
        raise ValueError(
            f"Archive URI {archive_uri!r} has no scheme -- prefix it "
            "explicitly, e.g. 'file:///path/to/archive.zip'."
        )
    return UriPath(archive_uri)


_registry_lock = _threading.Lock()
_registry: "_weakref.WeakValueDictionary[tuple[type, str], _ArchiveBackend]" = (
    _weakref.WeakValueDictionary()
)


def _get_backend(backend_cls: "type[_ArchiveBackend]", outer: "UriPath") -> "_ArchiveBackend":
    """Return the shared `_ArchiveBackend` for `outer`, creating one if this
    is the first live reference. Keyed by (backend class, outer URI string)
    so independently-constructed top-level `UriPath("zip:...")`/`"tar:..."`
    instances pointing at the same archive share one handle instead of each
    opening their own -- avoids the stale-read/corrupt-write hazard of two
    handles writing the same underlying file. Backed by a
    `WeakValueDictionary`: once every `ArchiveUri` referencing a given
    backend is garbage-collected, the backend itself is collected (its
    `__del__` closes the underlying handle) and the registry entry is
    dropped automatically -- no explicit refcounting needed."""
    key = (backend_cls, outer.as_uri())
    with _registry_lock:
        backend = _registry.get(key)
        if backend is None:
            backend = backend_cls(outer)
            _registry[key] = backend
        return backend


class _ArchiveBackend:
    """Lazily opens+caches the archive handle for one outer archive URI.
    Shared by every `ArchiveUri` instance derived from the same one, both
    through normal backend propagation (`ArchiveUri._init`,
    `with_segments`/`joinpath`/...) AND across independently-constructed
    top-level `UriPath(...)` instances via the module-level `_get_backend`
    registry above."""

    __slots__ = ("outer", "_handle", "__weakref__")

    def __init__(self, outer: "UriPath"):
        self.outer = outer
        self._handle = None

    def __del__(self):
        handle = self._handle
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def _open(self):
        raise NotImplementedError

    @property
    def handle(self):
        if self._handle is None:
            self._handle = self._open()
        return self._handle

    @property
    def writable(self) -> bool:
        return False

    def names(self) -> "list[str]":
        raise NotImplementedError

    def read_member(self, path: str):
        raise NotImplementedError

    def member_stat(self, path: str) -> FileStat:
        raise NotImplementedError


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


class _TarBackend(_ArchiveBackend):
    __slots__ = ()

    def _open(self):
        return _tarfile.open(fileobj=_io.BytesIO(self.outer.read_bytes()), mode="r:*")

    def names(self):
        return self.handle.getnames()

    def read_member(self, path):
        try:
            member = self.handle.getmember(path)
        except KeyError:
            raise
        f = self.handle.extractfile(member)
        if f is None:
            raise IsADirectoryError(path)
        return f

    def member_stat(self, path):
        info = self.handle.getmember(path)
        return FileStat(st_size=info.size, st_mtime=int(info.mtime), is_dir=info.isdir())


class ArchiveUri(UriPath):
    """Common base for `zip:`/`tar:` archive paths:
    `<scheme>:<archive-uri>!/<inner-path>` (Java-style separator; the
    `<archive-uri>` half is itself any absolute URI with an explicit scheme
    -- `file:`, `http:`, `sftp:`, `ftp:`, ... -- so archives are readable
    straight off any existing backend). `segments`/`name`/`parent`/`glob`/
    ... all operate on the *inner* path; the outer archive handle is the
    `backend` (`_ZipBackend`/`_TarBackend`), propagated through the normal
    backend machinery to every path derived from this one."""

    __slots__ = ()
    _backend_cls: type = None

    def _init(self, source, path, query, fragment, /, **kwargs):
        backend = kwargs.get("backend", None) or self._backend
        if backend is None:
            # Fresh top-level construction (e.g. UriPath("zip:...!/...")) --
            # `path` is still the raw "<archive-uri>!/<inner>" string.
            archive_str, inner = _split_archive_path(path)
            backend = _get_backend(self._backend_cls, _open_outer(archive_str))
        else:
            # Derived instance (with_segments/joinpath/_make_child_relpath)
            # -- backend already known, `path` is already just the inner
            # path (no "archive!/" prefix to strip).
            inner = path
        kwargs["backend"] = backend
        super()._init(source, inner, query, fragment, **kwargs)

    def as_uri(self, /, sanitize=False):
        outer_uri = self.backend.outer.as_uri(sanitize=sanitize)
        return f"{self.source.scheme}:{outer_uri}!/{self.path}"

    def _names(self):
        return self.backend.names()

    def _listdir(self):
        prefix = f"{self.path}/" if self.path else ""
        seen = set()
        for name in self._names():
            if not name.startswith(prefix):
                continue
            rest = name[len(prefix) :]
            if not rest:
                continue
            child = rest.split("/", 1)[0]
            if child and child not in seen:
                seen.add(child)
                yield child

    def stat(self, *, follow_symlinks=True):
        path = self.path
        if path == "":
            return FileStat(is_dir=True)
        names = self._names()
        if path in names:
            return self.backend.member_stat(path)
        dirmarker = f"{path}/"
        if dirmarker in names or any(n.startswith(dirmarker) for n in names):
            return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _open(self, mode="r", buffering=-1):
        if "r" in mode:
            try:
                return self.backend.read_member(self.path)
            except KeyError as error:
                raise FileNotFoundError(self) from error
        raise NotImplementedError(f"open(mode={mode!r}) on {self.source.scheme}:")


class _ZipWriteStream(_io.BytesIO):
    """Buffers a new entry's content in memory; on close(), writes it via
    `writestr()` and flushes+reopens the archive (see
    `_ZipBackend.write_member`)."""

    def __init__(self, backend: "_ZipBackend", path: str):
        super().__init__()
        self._backend = backend
        self._path = path

    def close(self):
        if not self.closed:
            self._backend.write_member(self._path, self.getvalue())
        super().close()


class ZipUri(ArchiveUri):
    """`zip:` scheme. Read/write: write support (new entries, overwriting
    existing entries, `unlink`/`rmdir`/`rename`) works when the outer
    archive is a local `file:` URI; every other outer scheme is read-only
    (fetched fully into memory first). New entries are appended in place
    (cheap); overwriting/deleting/renaming an existing entry requires a
    full-archive rewrite (`_ZipBackend._rewrite`) since `zipfile` has no
    in-place entry mutation -- each such call rewrites the whole archive to
    a temp file and atomically replaces the original (`os.replace`)."""

    __SCHEMES = ("zip",)
    __slots__ = ()
    _backend_cls = _ZipBackend

    def _require_writable(self):
        if not self.backend.writable:
            raise NotImplementedError(
                "zip: write support requires a local (file:) outer archive"
            )

    def _open(self, mode="r", buffering=-1):
        if "r" in mode:
            return super()._open(mode, buffering)
        self._require_writable()
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _ZipWriteStream(self.backend, self.path)

    def _mkdir(self, mode):
        self._require_writable()
        if self.exists():
            raise FileExistsError(self)
        self.backend.write_member(f"{self.path}/", b"")

    def unlink(self, missing_ok=False):
        self._require_writable()
        path = self.path
        if path not in self._names():
            if missing_ok:
                return
            raise FileNotFoundError(self)
        self.backend.delete_member(path)

    def rmdir(self):
        self._require_writable()
        path = self.path
        marker = f"{path}/"
        names = self._names()
        if any(n != marker and n.startswith(marker) for n in names):
            raise OSError(f"Directory not empty: {self}")
        if marker not in names:
            raise FileNotFoundError(self)
        self.backend.delete_member(marker)

    def rename(self, target: "ZipUri | Uri | str"):
        # A plain str target is a sibling rename (relative to self's
        # parent), matching sftp.py's/ftp.py's rename() semantics.
        self._require_writable()
        if not isinstance(target, Uri):
            target = Uri(self.parent, target)
        old_path = self.path
        new_path = target.path.lstrip("/")
        names = self._names()
        marker = f"{old_path}/"
        if old_path in names:
            self.backend.rename_member(old_path, new_path)
        elif marker in names or any(n.startswith(marker) for n in names):
            self.backend.rename_member(marker, f"{new_path}/")
        else:
            raise FileNotFoundError(self)


class TarUri(ArchiveUri):
    """`tar:` scheme (also handles `.tar.gz`/`.tar.bz2`/`.tar.xz` via
    `tarfile`'s auto-detected "r:*" mode). Read-only."""

    __SCHEMES = ("tar",)
    __slots__ = ()
    _backend_cls = _TarBackend
