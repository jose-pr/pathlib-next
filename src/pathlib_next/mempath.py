from __future__ import annotations

import io
import posixpath as _posix
from io import IOBase
from urllib.parse import quote as _urlquote

from .path import Path, Pathname
from .utils.stat import FileStat


class MemPathBackend(dict):
    """Nested-dict storage backing one or more `MemPath` trees. A `dict`
    value is a directory; a `bytearray` value is a file's content. Share
    one instance across `MemPath`s (via `backend=`) to give them the same
    virtual filesystem."""


class MemBytesIO(io.BytesIO):
    """A `BytesIO` that writes its buffer back into the backing
    `bytearray` (`dest`) on close, so `MemPath` files persist across
    `open()` calls."""

    def __init__(self, dest: bytearray) -> None:
        self._bytes = dest
        super().__init__()

    def close(self) -> None:
        # getvalue(), not seek(0);read(): a caller that seeks before
        # closing (or opened in append mode, positioned at EOF) would
        # otherwise lose everything before the current position.
        content = self.getvalue()
        self._bytes.clear()
        self._bytes.extend(content)
        return super().close()


class MemPath(Path):
    """In-memory `Path` implementation over nested dicts (see
    `MemPathBackend`) -- a lightweight virtual filesystem for mocks, tests,
    or transient storage, and the reference exemplar for Track A of
    extending this library (subclassing `Path` directly; see
    `docs/guides/extending.md`)."""

    __slots__ = ("_backend", "_segments", "_normalized")

    def __init__(
        self, *segments: str | Pathname | Path, backend: MemPathBackend = None, **kwargs
    ):
        _segments = []
        _backend = None
        for segment in segments:
            if isinstance(segment, MemPath):
                _segments.extend(segment.segments)
                _backend = segment.backend
            elif isinstance(segment, Path):
                raise NotImplementedError()
            elif isinstance(segment, Pathname):
                _segments.extend(segment.segments)
            else:
                _segments.append(segment)
        self._segments = "/".join(_segments).split("/")
        # `is not None`, not truthiness: a freshly-created root's backend is
        # an *empty* dict, which is falsy -- `if _backend:` silently treated
        # that as "no backend found" and gave the child a disconnected new
        # one, breaking backend sharing for any join off an empty MemPath.
        if _backend is not None and backend is None:
            backend = _backend
        self._backend = backend if backend is not None else MemPathBackend()
        self._normalized = None

    def __repr__(self):
        return "{}({!r})".format(type(self).__name__, self.as_posix())

    def __str__(self) -> str:
        return self.as_posix()

    @property
    def backend(self):
        return self._backend

    @property
    def normalized(self):
        if self._normalized is None:
            # Normalize against a virtual root ("/" + posix) so ".."-escaping
            # paths (e.g. "..", "../x") get clamped at the root instead of
            # mangling into "." (posixpath.normpath("..") == "..", and the
            # old .removeprefix(".") turned that into a bare "."). Strip any
            # existing leading "/" first: posixpath.normpath("//...") treats
            # an exactly-double-leading-slash specially (POSIX
            # implementation-defined root) and doesn't collapse it, which
            # broke MemPath("/") (as_posix() == "/") into a bogus "//".
            posix = self.as_posix().lstrip("/")
            self._normalized = (
                _posix.normpath("/" + posix).removeprefix("/").split("/")
            )
        return self._normalized

    @property
    def segments(self):
        return self._segments

    @property
    def parts(self):
        return self.segments, self.backend

    @property
    def parent(self):
        segments = self.segments[:-1]
        if segments == self.segments:
            return self
        return self.with_segments(*segments)

    def relative_to(self, other):
        raise NotImplementedError()

    def with_segments(self, *segments: str):
        return type(self)(*segments, backend=self.backend)

    def as_uri(self):
        return f"mempath:{_urlquote(self.as_posix())}"

    def _parent_container(self) -> tuple[dict[str, bytearray], str]:
        parent = self.backend
        *ancestors, name = self.normalized
        for path in ancestors:
            if path not in parent:
                raise FileNotFoundError(self.parent)
            else:
                parent = parent[path]

        return parent, name

    def _mkdir(self, mode: int):
        parent, name = self._parent_container()
        if not name or name in parent:
            raise FileExistsError(name)
        parent[name] = {}

    def rmdir(self):
        parent, name = self._parent_container()
        if not name:
            raise FileNotFoundError(self)
        content = parent.get(name)
        if content is None:
            raise FileNotFoundError(self)
        elif not isinstance(content, dict):
            raise NotADirectoryError(self)
        elif len(content) != 0:
            raise FileExistsError(self)
        parent.pop(name)

    def unlink(self, missing_ok=False):
        parent, name = self._parent_container()
        if not name:
            if missing_ok:
                return
            raise FileNotFoundError(self)
        content = parent.get(name)
        if content is None:
            if missing_ok:
                return
            raise FileNotFoundError(self)
        elif isinstance(content, dict):
            raise IsADirectoryError(self)
        parent.pop(name)

    def stat(self, *, follow_symlinks=True):
        parent, name = self._parent_container()
        if not name:
            return FileStat(is_dir=True)

        if name not in parent:
            raise FileNotFoundError(self)

        content = parent[name]
        is_dir = isinstance(content, dict)
        # st_size was never set for files (always defaulted to 0), which
        # silently broke any size-based checksum (e.g. PathSyncer's default
        # usage pattern).
        return FileStat(is_dir=is_dir, st_size=0 if is_dir else len(content))

    def iterdir(self):
        parent, name = self._parent_container()
        content = parent.get(name) if name else parent
        cls = type(self)

        if not isinstance(content, dict):
            raise NotADirectoryError(self)
        for c in list(content.keys()):
            yield cls(*self.segments, c, backend=self.backend)

    def _open(self, mode="r", buffering=-1) -> IOBase:
        # mode contract: "r"/"w" are required; "x"/"a" are supported here
        # as an extension. Anything else raises NotImplementedError.
        parent, name = self._parent_container()
        if mode == "r":
            if name not in parent:
                raise FileNotFoundError(self)
            content = parent[name]
            if isinstance(content, dict):
                raise IsADirectoryError(self)
            return io.BytesIO(content)
        elif mode == "w":
            content = bytearray()
            parent[name] = content
            return MemBytesIO(content)
        elif mode == "x":
            if name in parent:
                raise FileExistsError(self)
            content = bytearray()
            parent[name] = content
            return MemBytesIO(content)
        elif mode == "a":
            content = parent.setdefault(name, bytearray())
            if isinstance(content, dict):
                raise IsADirectoryError(self)
            buf = MemBytesIO(content)
            buf.write(content)
            return buf
        else:
            raise NotImplementedError(f"mode={mode!r}")
