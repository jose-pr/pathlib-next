from __future__ import annotations

import io as _io
import re as _re
import tarfile as _tarfile
import time as _time
import zipfile as _zipfile

from ...utils.stat import FileStat
from .. import UriPath
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


class _ArchiveBackend:
    """Lazily opens+caches the archive handle for one outer archive URI.
    Shared by every `ArchiveUri` instance derived from the same one (backend
    propagation is the existing `UriPath` machinery -- see `ArchiveUri._init`).
    NOT deduplicated across independently-constructed top-level `UriPath(...)`
    strings pointing at the same archive -- derive sibling members via `/`/
    `joinpath` from a shared instance rather than reparsing the same archive
    URI twice, especially for writable zips (two separate handles writing
    the same underlying file can corrupt it)."""

    __slots__ = ("outer", "_handle")

    def __init__(self, outer: "UriPath"):
        self.outer = outer
        self._handle = None

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
    __slots__ = ()

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
        return self.handle.namelist()

    def read_member(self, path):
        return self.handle.open(path, "r")  # raises KeyError if missing

    def write_member(self, path: str, data: bytes):
        # zipfile only persists the central directory to disk when the
        # *archive* (not just the entry) is closed -- close and drop the
        # cached handle so both this backend's next operation and any
        # independently-opened ZipUri see the write.
        self.handle.writestr(path, data)
        self.handle.close()
        self._handle = None

    def member_stat(self, path):
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
            backend = self._backend_cls(_open_outer(archive_str))
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
    """`zip:` scheme. Read/write: writing a new entry works when the outer
    archive is a local `file:` URI (opened in `zipfile`'s "a" -- append --
    mode, so existing entries are untouched); every other outer scheme is
    read-only (fetched fully into memory first). Overwriting/deleting/
    renaming an existing entry is not implemented (would require rewriting
    the whole archive) -- only adding new entries and (for directories)
    zero-length `"name/"` marker entries."""

    __SCHEMES = ("zip",)
    __slots__ = ()
    _backend_cls = _ZipBackend

    def _open(self, mode="r", buffering=-1):
        if "r" in mode:
            return super()._open(mode, buffering)
        if not self.backend.writable:
            raise NotImplementedError(
                "zip: write support requires a local (file:) outer archive"
            )
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _ZipWriteStream(self.backend, self.path)

    def _mkdir(self, mode):
        if not self.backend.writable:
            raise NotImplementedError(
                "zip: write support requires a local (file:) outer archive"
            )
        if self.exists():
            raise FileExistsError(self)
        self.backend.write_member(f"{self.path}/", b"")


class TarUri(ArchiveUri):
    """`tar:` scheme (also handles `.tar.gz`/`.tar.bz2`/`.tar.xz` via
    `tarfile`'s auto-detected "r:*" mode). Read-only."""

    __SCHEMES = ("tar",)
    __slots__ = ()
    _backend_cls = _TarBackend
