from __future__ import annotations

import errno as _errno
import io as _io
import os as _os
import re as _re
import threading as _threading
import weakref as _weakref

import uritools as _uritools

from ....utils import is_safe_child_name
from ....utils.stat import FileStat
from ... import Uri, UriPath
from ...source import _SAFE_PATH, Source
from ..file import FileUri

_SEP = "!/"
_SCHEME_RE = _re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_MEMBER_SEP_RE = _re.compile(r"[/\\]")
_DRIVE_RE = _re.compile(r"^[a-zA-Z]:")


def _is_safe_member_name(name: str) -> bool:
    """Whether archive member `name` stays inside the archive root once its
    parts are joined onto a destination (e.g. by a recursive `copy()`).

    Both `/` and `\\` count as separators here -- only here: lookups keep
    the raw member name, since `\\` is a legal filename character on POSIX.
    Rejects absolute names, empty/`.`/`..` parts and drive-qualified parts
    (`C:x`, `C:..`); a trailing `/` (a directory marker) is allowed."""
    if name.endswith("/"):
        name = name[:-1]
    return all(
        is_safe_child_name(part) and not _DRIVE_RE.match(part)
        for part in _MEMBER_SEP_RE.split(name)
    )


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


def _parse_archive_uri(raw: str, scheme: str) -> "tuple[str, str, object, str] | None":
    """Split a still percent-encoded `<scheme>:<archive-uri>!/<inner>`
    string at its first literal `!/` BEFORE anything is decoded, so each
    half is decoded exactly once: the outer by its own parse, the inner
    here. Returns (outer URI, decoded inner path, query, fragment), or None
    when `raw` does not start with `scheme`. An encoded `%21/` never
    separates, and the inner path's dot segments stop at the archive root."""
    prefix, sep, rest = raw.partition(":")
    if not sep or prefix.lower() != scheme:
        return None
    archive, inner = _split_archive_path(rest)
    _, path, query, fragment = Uri._parse_uri("/" + inner.lstrip("/"))
    return archive, path.lstrip("/"), query, fragment


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


def _local_outer_path(outer: "UriPath") -> "str | None":
    """The local filesystem path of a `file:` outer archive, else None."""
    if not isinstance(outer, FileUri):
        return None
    try:
        return str(outer.filepath)
    except (NotImplementedError, ValueError):
        return None


def _registry_key(backend_cls: type, outer: "UriPath") -> "tuple[type, str]":
    local = _local_outer_path(outer)
    if local is not None:
        # One key per file, not per spelling: `C:` vs `c:` or a symlink used
        # to get independent handles onto the same file.
        return backend_cls, _os.path.normcase(_os.path.realpath(local))
    return backend_cls, outer.as_uri()


def _get_backend(
    backend_cls: "type[_ArchiveBackend]", outer: "UriPath"
) -> "_ArchiveBackend":
    """Return the shared `_ArchiveBackend` for `outer`, creating one if this
    is the first live reference. Keyed by (backend class, canonical local
    file path -- or the outer URI string for a non-local outer) so independently-constructed top-level `UriPath("zip:...")`/`"tar:..."`
    instances pointing at the same archive share one handle instead of each
    opening their own -- avoids the stale-read/corrupt-write hazard of two
    handles writing the same underlying file. Backed by a
    `WeakValueDictionary`: once every `ArchiveUri` referencing a given
    backend is garbage-collected, the backend itself is collected (its
    `__del__` closes the underlying handle) and the registry entry is
    dropped automatically -- no explicit refcounting needed."""
    key = _registry_key(backend_cls, outer)
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
    registry above.

    For a local outer the cached handle is revalidated against the file's
    (inode, size, mtime) on every access and reopened when it changed, so a
    write by another process or tool is seen instead of being overwritten
    from a stale central directory. `_lock` serializes every use of the
    shared handle."""

    __slots__ = ("outer", "_handle", "_signature", "_lock", "__weakref__")

    def __init__(self, outer: "UriPath"):
        self.outer = outer
        self._handle = None
        self._signature = None
        self._lock = _threading.RLock()

    def __del__(self):
        try:
            self._close_handle()
        except Exception:
            pass

    def _open(self):
        raise NotImplementedError

    def _close_handle(self):
        # Drop the cached handle so the next operation (through any
        # instance sharing this backend) reopens and sees the change.
        handle = self._handle
        self._handle = None
        if handle is not None:
            handle.close()

    def _outer_signature(self):
        local = _local_outer_path(self.outer)
        if local is None:
            return None
        try:
            st = _os.stat(local)
        except OSError:
            return None
        return st.st_ino, st.st_size, st.st_mtime_ns

    @property
    def handle(self):
        with self._lock:
            signature = self._outer_signature()
            if self._handle is not None and signature != self._signature:
                self._close_handle()
            if self._handle is None:
                self._handle = self._open()
                self._signature = signature
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
    writable backends only). With `initial` (`open("r+")`) the buffer
    starts with the member's content at position 0 and is written back
    only if it was modified."""

    def __init__(
        self, backend: "_ArchiveBackend", path: str, initial: "bytes | None" = None
    ):
        super().__init__(b"" if initial is None else initial)
        self._backend = backend
        self._path = path
        self._dirty = initial is None

    def write(self, data):
        self._dirty = True
        return super().write(data)

    def writelines(self, lines):
        self._dirty = True
        return super().writelines(lines)

    def truncate(self, size=None):
        self._dirty = True
        return super().truncate(size)

    def close(self):
        if not self.closed:
            try:
                if self._dirty:
                    self._backend.write_member(self._path, self.getvalue())
            finally:
                # Closed even when the write fails, so `__del__` does not
                # retry it (and raise again) at garbage collection.
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
            # Fresh top-level construction (e.g. UriPath("zip:...!/...")).
            # Split the original, still-encoded string when there is one:
            # `path` has already been decoded (and dot-segment-normalized)
            # as a whole, which merged the outer's query into ours and
            # decoded the outer twice.
            raw = self._raw_uris
            parsed = None
            if raw and len(raw) == 1 and isinstance(raw[0], str):
                parsed = _parse_archive_uri(raw[0], source.scheme)
            if parsed is not None:
                archive_str, inner, query, fragment = parsed
            else:
                archive_str, inner = _split_archive_path(path)
                inner = inner.lstrip("/")
            outer = _open_outer(archive_str)
            # `ZipUri`/`TarUri` pin `_backend_cls`; the base `archive:`
            # scheme leaves it `None`, meaning "detect per outer archive".
            backend_cls = self._backend_cls or _detect_backend_cls(outer)
            backend = _get_backend(backend_cls, outer)
        else:
            # Derived instance (with_segments/joinpath/_make_child_relpath)
            # -- backend already known, `path` is already just the inner
            # path (no "archive!/" prefix to strip). Member names never
            # start with "/", but the generic `_make_child_relpath` joins a
            # child of the root (path "") as "/name".
            inner = path.lstrip("/")
        kwargs["backend"] = backend
        super()._init(source, inner, query, fragment, **kwargs)

    def __new__(cls, *args, **kwargs):
        inst = super().__new__(cls, *args, **kwargs)
        if len(args) == 1 and isinstance(args[0], str) and not inst._raw_uris:
            # Keep the undecoded string for `_init` (see `_parse_archive_uri`).
            inst._raw_uris = [args[0]]
        return inst

    def as_uri(self, /, sanitize=False):
        if not self.source:
            # A derived relative path (`relative_to`): no archive to name.
            return super().as_uri(sanitize=sanitize)
        # Encoded so the string parses back to the same member: the inner
        # path's `%`, `?` and `#`, and a literal "!/" inside the outer URI.
        outer_uri = self.backend.outer.as_uri(sanitize=sanitize)
        outer_uri = outer_uri.replace(_SEP, "%21/")
        inner = _uritools.uriencode(self.path, _SAFE_PATH).decode()
        tail = self._format_parsed_parts(
            Source(None, None, None, None), "", self.query, self.fragment
        )
        return f"{self.source.scheme}:{outer_uri}{_SEP}{inner}{tail}"

    def _names(self):
        return self.backend.names()

    def _listdir(self):
        if self.path and not self.stat().is_dir():
            raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
        prefix = f"{self.path}/" if self.path else ""
        seen = set()
        for name in self._names():
            # An unsafe member ('../x', '/abs', 'C:x', ...) is never listed:
            # a caller joining a listed name onto a destination must stay in it.
            if not name.startswith(prefix) or not _is_safe_member_name(name):
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
        if dirmarker in names:
            return self.backend.member_stat(dirmarker)
        if any(n.startswith(dirmarker) for n in names):
            return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _is_dir_member(self) -> bool:
        try:
            return self.stat().is_dir()
        except FileNotFoundError:
            return False

    def _check_parent(self):
        """pathlib's check before creating `self`: the parent must exist and
        be a directory (a zip directory may be implicit, see `stat`)."""
        parent = self.parent
        if not parent.path:
            return
        try:
            is_dir = parent.stat().is_dir()
        except FileNotFoundError:
            raise FileNotFoundError(
                _errno.ENOENT, "No such file or directory", str(self)
            ) from None
        if not is_dir:
            raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))

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

    def _read_member(self):
        try:
            return self.backend.read_member(self.path)
        except KeyError as error:
            if self._is_dir_member():
                raise IsADirectoryError(
                    _errno.EISDIR, "Is a directory", str(self)
                ) from error
            raise FileNotFoundError(self) from error

    def _open(self, mode="r", buffering=-1):
        if "r" in mode and "+" not in mode:
            return self._read_member()
        self._require_writable()
        if "r" in mode:
            # Read-modify-write: what is written reaches the archive on close.
            data = self._read_member().read()
            return _ArchiveWriteStream(self.backend, self.path, initial=data)
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if self._is_dir_member():
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        self._check_parent()
        return _ArchiveWriteStream(self.backend, self.path)

    def _mkdir(self, mode):
        self._require_writable()
        if self.exists():
            raise FileExistsError(self)
        self._check_parent()
        self.backend.write_member(f"{self.path}/", b"")

    def unlink(self, missing_ok=False):
        self._require_writable()
        path = self.path
        if path not in self._names():
            if self._is_dir_member():
                raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
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
            raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
        if marker not in names:
            if path in names:
                raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
            raise FileNotFoundError(self)
        self.backend.delete_member(marker)

    def _same_location(self, other: Uri) -> bool:
        # Every archive URI has the same bare "zip:"/"tar:" authority, so the
        # authority alone cannot tell two archives apart: an archive path is
        # only renameable onto a member of the very same archive (backend).
        if isinstance(other, ArchiveUri):
            return other.backend is self.backend
        if isinstance(other, UriPath):
            return False
        return super()._same_location(other)

    def rename(self, target: "ArchiveUri | Uri | str"):
        # A plain str target is a sibling rename (relative to self's
        # parent), matching sftp.py's/ftp.py's rename() semantics. An existing
        # target is replaced as POSIX rename(2) does: a file replaces a file,
        # a directory replaces an empty directory -- never a duplicate member.
        # Returns the new path, as pathlib does.
        self._require_writable()
        target = self._rename_target(target)
        old_path = self.path
        new_path = target.path.lstrip("/")
        names = self._names()
        marker = f"{old_path}/"
        new_marker = f"{new_path}/"
        renamed = self._from_parsed_parts(self.source, new_path, "", "")
        if old_path in names:
            if new_path == old_path:
                return renamed
            if any(n.startswith(new_marker) for n in names):
                raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(target))
            self.backend.rename_member(old_path, new_path)
        elif any(n.startswith(marker) for n in names):
            if new_marker == marker:
                return renamed
            if new_path in names:
                raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(target))
            if any(n != new_marker and n.startswith(new_marker) for n in names):
                raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(target))
            self.backend.rename_member(marker, new_marker)
        else:
            raise FileNotFoundError(self)
        return renamed
