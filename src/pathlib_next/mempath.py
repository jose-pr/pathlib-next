import io
import posixpath as _posix
from io import IOBase
from urllib.parse import quote as _urlquote

from .path import Path, Pathname
from .utils.stat import FileStat, FileStatLike


class MemPathBackend(dict): ...


class MemBytesIO(io.BytesIO):
    def __init__(self, initial_bytes: bytearray) -> None:
        self._bytes = initial_bytes
        super().__init__(initial_bytes)

    def close(self) -> None:
        self.seek(0)
        self._bytes.clear()
        self._bytes.extend(self.read())
        return super().close()


class MemPath(Path):

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
        if _backend and backend is None:
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
            self._normalized = (
                _posix.normpath(self.as_posix())
                .removeprefix(".")
                .removeprefix("/")
                .split("/")
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

    def as_posix(self):
        return "/".join(self.segments)

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

    def stat(self, *, follow_symlinks=True) -> FileStatLike:
        parent, name = self._parent_container()
        if not name:
            return FileStat(is_dir=True)

        if name not in parent:
            return FileNotFoundError(self)

        return FileStat(is_dir=isinstance(parent[name], dict))

    def iterdir(self):
        parent, name = self._parent_container()
        content = parent.get(name) if name else parent
        cls = type(self)

        if not isinstance(content, dict):
            raise NotADirectoryError(self)
        for c in list(content.keys()):
            yield cls(*self.segments, c, backend=self.backend)

    def _open(self, mode="r", buffering=-1) -> IOBase:
        parent, name = self._parent_container()
        if "w" in mode:
            content = parent.setdefault(name, bytearray())
            return MemBytesIO(content)
        elif name not in parent:
            return FileNotFoundError(self)
        else:
            content = parent[name]

        return io.BytesIO(content)
