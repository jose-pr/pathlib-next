from __future__ import annotations

import errno as _errno
import io
import posixpath as _posix
import time as _time
from io import IOBase
from urllib.parse import quote as _urlquote

from .path import Path, Pathname
from .utils.stat import FileStat


class MemPathBackend(dict):
    """Nested-dict storage backing one or more `MemPath` trees. A `dict`
    value is a directory; a `bytearray` value is a file's content. Share
    one instance across `MemPath`s (via `backend=`) to give them the same
    virtual filesystem."""


class MemFile(bytearray):
    """A file's content in a `MemPathBackend`, carrying its modification
    time. A plain `bytearray` placed in a backend by hand still works; it
    reports `st_mtime` 0."""

    __slots__ = ("mtime",)

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.mtime = 0.0
        _touch(self)


def _touch(content) -> None:
    """Advance `content`'s mtime -- strictly, even within one clock tick:
    a sync quick check comparing (size, mtime) must see every write."""
    try:
        content.mtime = max(_time.time(), getattr(content, "mtime", 0.0) + 1e-6)
    except AttributeError:
        pass


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
        if not self.closed:
            content = self.getvalue()
            self._bytes.clear()
            self._bytes.extend(content)
            _touch(self._bytes)
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
        # Joined and normalized like `PurePosixPath`: empty and "." segments
        # collapse, and an absolute argument restarts the join. Raw
        # concatenation made `MemPath("/") / "a"` the path "//a", unequal to
        # (and hashed apart from) `MemPath("/a")`, gave "d/" an empty name,
        # and let `MemPath("/root") / "/etc"` address "/root/etc".
        path = ""
        _backend = None
        for segment in segments:
            if isinstance(segment, MemPath):
                text = segment.as_posix()
                _backend = segment.backend
            elif isinstance(segment, Path):
                raise NotImplementedError()
            elif isinstance(segment, Pathname):
                text = "/".join(segment.segments)
            elif isinstance(segment, str):
                text = segment
            else:
                raise TypeError(
                    "argument should be a str or a Pathname, "
                    f"not {type(segment).__name__!r}"
                )
            if text.startswith("/") or not path:
                path = text
            elif text:
                path = f"{path}/{text}"
        self._segments = self._parse(path)
        # `is not None`, not truthiness: a freshly-created root's backend is
        # an *empty* dict, which is falsy -- `if _backend:` silently treated
        # that as "no backend found" and gave the child a disconnected new
        # one, breaking backend sharing for any join off an empty MemPath.
        if _backend is not None and backend is None:
            backend = _backend
        self._backend = backend if backend is not None else MemPathBackend()
        self._normalized = None

    @staticmethod
    def _parse(path: str) -> list:
        """Segments of a joined path string: `["", ""]` for the root,
        `["", name, ...]` for another absolute path, `[name, ...]` for a
        relative one and `[]` for the empty path (so its `root` is "")."""
        names = [name for name in path.split("/") if name and name != "."]
        if path.startswith("/"):
            return ["", *names] if names else ["", ""]
        return names

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
            self._normalized = _posix.normpath("/" + posix).removeprefix("/").split("/")
        return self._normalized

    @property
    def segments(self):
        return self._segments

    @property
    def parts(self):
        return self.segments, self.backend

    @property
    def parent(self):
        segments = self.segments
        if not segments or segments == ["", ""]:
            return self
        if len(segments) == 2 and segments[0] == "":
            # "/a" -> "/": dropping the root gave the relative empty path.
            return self.with_segments("", "")
        return self.with_segments(*segments[:-1])

    def relative_to(self, other):
        raise NotImplementedError()

    def with_segments(self, *segments: str):
        if all(isinstance(segment, str) for segment in segments):
            # The `Pathname` protocol's spelling: segments joined with "/",
            # a leading "" marking the root (`("", "a")` is "/a").
            segments = ("/".join(segments),)
        return type(self)(*segments, backend=self.backend)

    def as_uri(self):
        return f"mempath:{_urlquote(self.as_posix())}"

    def _parent_container(self) -> tuple[dict[str, bytearray], str]:
        parent = self.backend
        *ancestors, name = self.normalized
        for index, path in enumerate(ancestors):
            if path not in parent:
                raise FileNotFoundError(self.parent)
            parent = parent[path]
            if not isinstance(parent, dict):
                # An ancestor segment names a file. Without this the next
                # iteration evaluates `"seg" not in bytearray` and raises
                # TypeError, which sails past the OSError guards in
                # stat()/exists()/is_dir() -- so even exists() crashed on a
                # path merely routed through a file. NotADirectoryError is
                # an OSError, which is what stdlib raises and what those
                # guards already swallow.
                raise NotADirectoryError(self.with_segments(*ancestors[: index + 1]))

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
            # pathlib raises OSError(ENOTEMPTY); FileExistsError (EEXIST)
            # carried no errno and matched no caller's ENOTEMPTY check.
            raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
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
        return FileStat(
            is_dir=is_dir,
            st_size=0 if is_dir else len(content),
            st_mtime=getattr(content, "mtime", 0),
        )

    def iterdir(self):
        parent, name = self._parent_container()
        content = parent.get(name) if name else parent
        if content is None:
            raise FileNotFoundError(self)
        if not isinstance(content, dict):
            raise NotADirectoryError(self)
        for c in list(content.keys()):
            yield self.with_segments(*self.segments, c)

    def _open(self, mode="r", buffering=-1) -> IOBase:
        # mode contract: "r"/"w" are required; "x"/"a" are supported here
        # as an extension. Anything else raises NotImplementedError.
        parent, name = self._parent_container()
        if not name:
            # An empty name is the virtual root, which stat() reports as a
            # directory. Without this guard "w"/"a" created a bogus ""
            # entry in the backend and "r" claimed FileNotFoundError.
            raise IsADirectoryError(self)
        if mode == "r":
            if name not in parent:
                raise FileNotFoundError(self)
            content = parent[name]
            if isinstance(content, dict):
                raise IsADirectoryError(self)
            return io.BytesIO(content)
        elif mode == "w":
            if isinstance(parent.get(name), dict):
                # Truncating over a directory silently replaced the whole
                # subtree with a file; stdlib raises IsADirectoryError.
                raise IsADirectoryError(self)
            content = parent.get(name)
            if isinstance(content, bytearray):
                # Truncate the same file, as "w" does on disk: its mtime
                # then still advances past the previous write's.
                content.clear()
                _touch(content)
            else:
                content = MemFile()
                parent[name] = content
            return MemBytesIO(content)
        elif mode == "x":
            if name in parent:
                raise FileExistsError(self)
            content = MemFile()
            parent[name] = content
            return MemBytesIO(content)
        elif mode == "a":
            content = parent.setdefault(name, MemFile())
            if isinstance(content, dict):
                raise IsADirectoryError(self)
            buf = MemBytesIO(content)
            buf.write(content)
            return buf
        else:
            raise NotImplementedError(f"mode={mode!r}")
