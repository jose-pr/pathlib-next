from __future__ import annotations

import io as _io
import re as _re
import threading as _threading
import weakref as _weakref

from ....utils.stat import FileStat
from ... import Uri, UriPath

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


def _detect_backend_cls(outer: "UriPath") -> type:
    """Detect zip vs tar for `outer` (extension, then magic-byte sniff --
    see `utils.archive._detect_format`) and return the matching backend
    class. Lazily imports `.zip`/`.tar` (rather than at module level) to
    avoid a circular import: both submodules import `ArchiveUri`/
    `_ArchiveBackend` from this module, so this module can't import them
    back at load time -- only safe once this module has finished loading,
    which it always has by the time `_init` (the only caller) runs."""
    from ....utils.archive import _detect_format
    from .tar import _TarBackend
    from .zip import _ZipBackend

    def _peek() -> bytes:
        try:
            with outer.open("rb") as f:
                return f.read(4)
        except Exception:
            return b""

    fmt = _detect_format(outer.name, _peek)
    return _ZipBackend if fmt == "zip" else _TarBackend


class _ArchiveWriteStream(_io.BytesIO):
    """Buffers a new entry's content in memory; on close(), writes it via
    the backend's `write_member()` (only `_ZipBackend` implements it --
    `ArchiveUri._require_writable()` gates construction of this stream to
    writable backends only)."""

    def __init__(self, backend: "_ArchiveBackend", path: str):
        super().__init__()
        self._backend = backend
        self._path = path

    def close(self):
        if not self.closed:
            self._backend.write_member(self._path, self.getvalue())
        super().close()


class ArchiveUri(UriPath):
    """Common base for `zip:`/`tar:`/`archive:` archive paths:
    `<scheme>:<archive-uri>!/<inner-path>` (Java-style separator; the
    `<archive-uri>` half is itself any absolute URI with an explicit scheme
    -- `file:`, `http:`, `sftp:`, `ftp:`, ... -- so archives are readable
    straight off any existing backend). `segments`/`name`/`parent`/`glob`/
    ... all operate on the *inner* path; the outer archive handle is the
    `backend` (`_ZipBackend`/`_TarBackend`), propagated through the normal
    backend machinery to every path derived from this one.

    Also registered directly as the `archive:` catch-all scheme (see
    `__SCHEMES` below): `zip:`/`tar:` (via `ZipUri`/`TarUri`, which just
    pin `_backend_cls`) fix the format; plain `archive:` auto-detects it
    per-instance in `_init` when `_backend_cls` is left at its `None`
    sentinel. Write methods below are format-agnostic -- gated on
    `self.backend.writable`, which only `_ZipBackend` (and only for a
    local `file:` outer) ever reports `True` -- so a tar-backed instance,
    whether reached via `tar:` or auto-detected via `archive:`, correctly
    raises `NotImplementedError` on any write attempt rather than
    silently misbehaving."""

    __SCHEMES = ("archive",)
    __slots__ = ()
    _backend_cls: type = None

    def _init(self, source, path, query, fragment, /, **kwargs):
        backend = kwargs.get("backend", None) or self._backend
        if backend is None:
            # Fresh top-level construction (e.g. UriPath("zip:...!/...")) --
            # `path` is still the raw "<archive-uri>!/<inner>" string.
            archive_str, inner = _split_archive_path(path)
            outer = _open_outer(archive_str)
            # `ZipUri`/`TarUri` pin `_backend_cls`; the base `archive:`
            # scheme leaves it `None`, meaning "detect per outer archive".
            backend_cls = self._backend_cls or _detect_backend_cls(outer)
            backend = _get_backend(backend_cls, outer)
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

    def _require_writable(self):
        backend = self.backend
        if getattr(backend, "writable", False):
            return
        if hasattr(backend, "write_member"):
            # A write-capable backend type (currently only _ZipBackend),
            # just not writable for *this* outer (non-local outer URI).
            raise NotImplementedError(
                f"{self.source.scheme}: write support requires a local "
                "(file:) outer archive"
            )
        raise NotImplementedError(
            f"{self.source.scheme}: write support is only available for "
            "zip-format archives"
        )

    def _open(self, mode="r", buffering=-1):
        if "r" in mode:
            try:
                return self.backend.read_member(self.path)
            except KeyError as error:
                raise FileNotFoundError(self) from error
        self._require_writable()
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _ArchiveWriteStream(self.backend, self.path)

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

    def rename(self, target: "ArchiveUri | Uri | str"):
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
