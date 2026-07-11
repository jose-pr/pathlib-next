from __future__ import annotations

import os
import pathlib as _pathlib
import posixpath as _posix
import typing as _ty

import uritools

if _ty.TYPE_CHECKING:
    from typing import Self, TypeAlias
else:
    TypeAlias = _ty.Any

from .. import utils as _utils
from ..path import Path, Pathname
from ..utils.stat import FileStat
from .query import Query
from .source import Source

UriLike: TypeAlias = "str | Uri | os.PathLike"

_NOSOURCE = Source(None, None, None, None)

_U = _ty.TypeVar("_U", bound="Uri")


def _segments_of(path: str) -> list[str]:
    """Split a normalized posix-style path into segments for prefix
    comparison. "/" alone must yield [""] (the root), not ["", ""]
    (str.split's artifact for a string ending in "/")."""
    if path == "/":
        return [""]
    return path.split("/")


def _uriencode(text: str, safe=""):
    return uritools.uriencode(text, safe=safe).decode()


class Uri(Pathname):
    """A pure (no I/O) RFC 3986 URI, lazily parsed into `source` (scheme/
    userinfo/host/port), `path`, `query`, and `fragment` on first access.
    Join semantics (multiple constructor args, or `/`) are pathlib-
    `joinpath`-like, not RFC 3986 reference resolution -- see
    `_load_parts`'s docstring and `docs/divergences.md`."""

    __slots__ = (
        "_raw_uris",
        "_source",
        "_path",
        "_query",
        "_fragment",
        "_uri",
        "_initiated",
        "_normalized_path",
        "_segments_cache",
        "_suffix_cache",
        "_stem_cache",
    )

    def __new__(cls, *uris, **options):
        inst = object.__new__(cls)
        for cls in cls.__mro__:
            for slot in getattr(cls, "__slots__", ()):
                if not hasattr(inst, slot):
                    setattr(inst, slot, None)
        return inst

    def __init__(self, *uris: UriLike, **options):
        if self._raw_uris or self._initiated:
            return
        _uris: list[str | Uri] = []
        for uri in uris:
            if not uri:
                uri = ""
            if isinstance(uri, Uri):
                _uris.append(uri)
            elif isinstance(uri, (_pathlib.Path, Path)):
                try:
                    uri = uri.as_uri()
                except ValueError:
                    # as_uri() raises ValueError for a relative path.
                    uri = f"file:{_uriencode(uri.as_posix(), safe='/')}"
                _uris.append(uri)
            elif isinstance(uri, (_pathlib.PurePath, Pathname)):
                _uris.append(f"{_uriencode(uri.as_posix(), safe='/')}")
            elif hasattr(uri, "as_uri"):
                path = uri.as_uri
                if callable(path):
                    path = path()
                _uris.append(path)
            elif isinstance(uri, str):
                _uris.append(uri)
            elif isinstance(uri, bytes):
                _uris.append(uri.decode())
            else:
                path = None
                try:
                    path = os.fspath(uri)
                except (TypeError, NotImplementedError):
                    pass
                if not isinstance(path, str):
                    raise TypeError(
                        "argument should be a str or an os.PathLike "
                        "object where __fspath__ returns a str, "
                        f"not {type(path).__name__!r}"
                    )
                # Only __fspath__ is guaranteed here -- posix-normalize the
                # string itself rather than assuming an as_posix() method.
                posix = _pathlib.PurePath(path).as_posix()
                _uris.append(f"{_uriencode(posix, safe='/')}")
        self._raw_uris = _uris

    @classmethod
    def _parse_uri(cls, uri: str) -> tuple[Source, str, Query, str]:
        parsed = uritools.urisplit(uri)
        return (
            Source(
                parsed.getscheme(),
                parsed.getuserinfo(),
                parsed.gethost() or "",
                parsed.getport(),
            ),
            parsed.getpath(),
            Query(parsed.getquery() or ""),
            parsed.getfragment() or "",
        )

    @property
    def parts(self):
        return (self.source, self.path, self.query, self.fragment)

    def _load_parts(self):
        """Join semantics (B26, documented -- this is deliberate, not RFC
        3986 reference resolution): multiple constructor arguments are
        joined pathlib-`joinpath`-style, right to left, stopping at the
        first absolute segment. `..` is never resolved during join (unlike
        RFC 3986 relative-reference resolution). `source` is taken from the
        last (rightmost) segment that has one; `query`/`fragment` likewise
        come from the last segment that actually sets one (a later
        segment with no query/fragment does not blank out an earlier one).
        """
        uris = self._raw_uris
        source = _NOSOURCE
        query = fragment = None
        _path = ""

        if not uris:
            pass
        elif len(uris) == 1 and isinstance(uris[0], Uri):
            source, _path, query, fragment = uris[0].parts
        else:
            paths: list[str] = []
            for _uri in uris:
                src, path, q, frag = (
                    _uri.parts if isinstance(_uri, Uri) else self._parse_uri(_uri)
                )
                if bool(src):
                    source = src
                if q:
                    query = q
                if frag:
                    fragment = frag
                paths.append(path)

            for path in reversed(paths):
                if not path:
                    continue
                if path.endswith("/"):
                    _path = f"{path}{_path}"
                elif _path:
                    _path = f"{path}/{_path}"
                else:
                    _path = path
                if _path.startswith("/"):
                    break

        if (
            (source.host or source.userinfo or source.port)
            and _path
            and not _path.startswith("/")
        ):
            _path = "/" + _path

        self._init(source, _path, query, fragment)

    def _init(self, source: Source, path: str, query: str, fragment: str, **kwargs):
        # Re-init on an already-initiated instance is intentional and
        # relied upon (e.g. UriPath.with_source() constructs via __new__,
        # which already calls _init() once, then calls it again to
        # overwrite with the new source) -- do not turn this into a raise
        # without auditing every _init() call site first.
        self._initiated = True
        self._source = source
        self._path = path
        self._query = query
        self._fragment = fragment
        self._segments_cache = None
        self._suffix_cache = None
        self._stem_cache = None

    def _from_parsed_parts(
        self, source: Source, path: str, query: str, fragment: str, /, **kwargs
    ):
        cls = type(self)
        uri = cls.__new__(cls)
        uri._init(source, path, query, fragment, **kwargs)
        return uri

    @classmethod
    def _format_parsed_parts(
        cls,
        source: Source,
        path: str,
        query: str,
        fragment: str,
        /,
        sanitize=True,
    ) -> str:
        parts = {
            "path": path,
        }
        if query:
            parts["query"] = query
        if fragment:
            parts["fragment"] = fragment
        if source:
            source_ = source._asdict()
            if sanitize:
                source_["userinfo"] = (source_["userinfo"] or "").split(
                    ":", maxsplit=1
                )[0]
            parts.update(source_)

        return uritools.uricompose(**{k: v for k, v in parts.items() if v})

    def __str__(self):
        """Return the string representation of the path, suitable for
        passing to system calls."""
        return self.as_uri(sanitize=True)

    def __fspath__(self):
        if (self.source.scheme or "file") == "file":
            if not self.source.host or self.is_local():
                return self.path
            elif os.name == "nt":
                return f"//{self.source.host}/{self.path.removeprefix('/')}"
            else:
                raise NotImplementedError(f"OS Support for not local fspath")

        raise NotImplementedError(f"fspath for {self.source.scheme}")

    def __repr__(self):
        if self._initiated:
            return "{}({!r})".format(type(self).__name__, str(self))
        else:
            return super().__repr__()

    def as_uri(self, /, sanitize=False):
        if self._uri is None or sanitize:
            uri = self._format_parsed_parts(
                self.source, self.path, self.query, self.fragment, sanitize=sanitize
            )
            if not sanitize:
                self._uri = uri
            return uri
        else:
            return self._uri

    @property
    def source(self) -> Source:
        if not self._initiated:
            self._load_parts()
        return self._source

    @property
    def path(self) -> str:
        if not self._initiated:
            self._load_parts()
        return self._path

    @property
    def query(self) -> str:
        if not self._initiated:
            self._load_parts()
        return self._query

    @property
    def fragment(self) -> str:
        if not self._initiated:
            self._load_parts()
        return self._fragment

    def _make_child_relpath(self, name: str, **kwargs) -> _ty.Self:
        cls = type(self)
        inst = cls.__new__(cls)
        # Ensure exactly one "/" joins path and name -- a directory's own
        # path conventionally carries a trailing "/" for some schemes
        # (http/dav listings, or any Uri explicitly constructed that way);
        # joining against it unconditionally (the old `f"{self.path}/{name}"`)
        # doubled the slash (e.g. path="/" + name "sub" => "//sub").
        path = self.path
        if not path:
            # An empty path with an authority present is the same root as
            # "/" (RFC 3986: "http://host" and "http://host/" are
            # equivalent) -- treat it the same way so a child gets
            # "/name", not a bare, schemeless-looking "name".
            new_path = f"/{name}" if self.source else name
        else:
            new_path = (path if path.endswith("/") else f"{path}/") + name
        inst._init(self.source, new_path, "", "", **kwargs)
        return inst

    def with_source(self, source: Source):
        return self._from_parsed_parts(source, self.path, self.query, self.fragment)

    def with_segments(self, *segments: str):
        if not segments:
            return self.with_path("")
        return self.with_path("/".join(segments))

    def with_path(self, path: str | Pathname):
        return self._from_parsed_parts(
            self.source,
            path.as_posix() if isinstance(path, Pathname) else path,
            self.query,
            self.fragment,
        )

    def with_query(self, query: str):
        if not isinstance(query, Query):
            query = Query(query)
        return self._from_parsed_parts(self.source, self.path, query, self.fragment)

    def with_fragment(self, fragment: str):
        return self._from_parsed_parts(self.source, self.path, self.query, fragment)

    @property
    def segments(self):
        if self._segments_cache is None:
            if not self.path:
                self._segments_cache = ()
            else:
                self._segments_cache = tuple(self.path.split("/"))
        return self._segments_cache

    @property
    def suffix(self) -> str:
        if self._suffix_cache is None:
            name = self.name
            i = name.rfind(".")
            if 0 < i < len(name) - 1:
                self._suffix_cache = name[i:]
            else:
                self._suffix_cache = ""
        return self._suffix_cache

    @property
    def stem(self) -> str:
        if self._stem_cache is None:
            name = self.name
            i = name.rfind(".")
            if 0 < i < len(name) - 1:
                self._stem_cache = name[:i]
            else:
                self._stem_cache = name
        return self._stem_cache

    @property
    def parent(self):
        """The logical parent of the path."""
        segments = self.segments
        if not segments or len(segments) == 2 and segments[1] == "":
            return self
        return self.with_path("/".join(segments[:-1]))

    @property
    def normalized_path(self):
        if self._normalized_path is None:
            self._normalized_path = _posix.normpath(self.path)
        return self._normalized_path

    def is_absolute(self):
        """True if the path is absolute."""
        return bool(self.source) and self.path.startswith("/")

    def is_relative_to(self, other: UriLike):
        """Return True if the path is relative to another path or False."""
        other = other if isinstance(other, Uri) else Uri(self, _ROOT, other)
        if not (
            (other.source == self.source)
            or not (bool(self.source) and bool(other.source))
        ):
            return False
        # Segment-wise prefix comparison: a naive startswith() on the raw
        # strings would report "/foo/bar2" as relative to "/foo/bar".
        _other = _segments_of(other.normalized_path)
        _self = _segments_of(self.normalized_path)
        return _self[: len(_other)] == _other

    def relative_to(self, other: UriLike, *, walk_up=False):
        other = other if isinstance(other, Uri) else Uri(other)
        # NOTE: no upfront `if not self.is_relative_to(other): raise` here --
        # that used to short-circuit before the walk_up loop below ever ran,
        # making walk_up=True dead code. The step==0 iteration of the loop
        # (path=other) reproduces the exact same non-walk_up error.
        for step, path in enumerate([other] + list(other.parents)):
            if self.is_relative_to(path):
                break
            elif not walk_up:
                raise ValueError(
                    f"{str(self)!r} is not in the subpath of {str(other)!r}"
                )
            elif path.name == "..":
                raise ValueError(f"'..' segment in {str(other)!r} cannot be walked")
        else:
            raise ValueError(f"{str(self)!r} and {str(other)!r} have different anchors")
        parts = [".."] * step + list(self.segments[len(path.segments) :])
        return self._from_parsed_parts(
            _NOSOURCE, "/".join(parts), self.query, self.fragment
        )

    def is_local(self):
        return self.source.is_local()

    def __eq__(self, other: Pathname | str):
        if isinstance(other, Pathname):
            uri = other.as_uri()
        elif isinstance(other, str):
            uri = other
        else:
            return NotImplemented
        return self.as_uri() == uri

    def __hash__(self):
        return hash(self.as_uri())

    def as_posix(self):
        source = self.source
        host = None
        posix = self.path
        if source.host:
            host = source.host
            user, password = source.parsed_userinfo()
            if user:
                posix = f"{user}@{host}:{posix}"
            else:
                posix = f"{host}:{posix}"
        return posix


class UriPath(Uri, Path):
    """`Uri` + `Path` (I/O) + scheme dispatch. `UriPath(...)` constructs
    the concrete subclass registered for the URI's scheme (via `__SCHEMES`)
    -- e.g. `UriPath("http://...")` returns an `HttpPath`. Subclass this
    and set `__SCHEMES` to add a new scheme (Track B of extending this
    library; see `docs/guides/extending.md`); implement the I/O surface
    (`_listdir` or `_scandir`, `stat`, `_open`, ...) documented in
    `AGENTS.md`. Prefer overriding `_scandir()` over `_listdir()` when the
    listing call already returns type/size/mtime metadata (PROPFIND, MLSD,
    `listdir_attr`, an S3 list page, ...) -- `walk()`/`glob()` then answer
    `is_dir()` on the results for free, without a stat request per entry."""

    __slots__ = ("_backend", "_stat_hint")
    __SCHEMES: _ty.Sequence[str] = ()
    __SCHEMESMAP: _ty.Mapping[str, type["Self"]] = None

    @classmethod
    def _schemesmap(cls, reload=False) -> _ty.Mapping[str, type["Self"]]:
        _propname = f"_{cls.__name__}__SCHEMESMAP"
        if not reload:
            try:
                schemesmap = getattr(cls, _propname)
                if schemesmap is not None:
                    return schemesmap
            except AttributeError:
                pass
        schemesmap = cls._get_schemesmap()
        setattr(cls, _propname, schemesmap)
        return schemesmap

    @classmethod
    def _schemes(cls) -> _ty.Sequence[str]:
        try:
            return getattr(cls, f"_{cls.__name__}__SCHEMES")
        except AttributeError as _e:
            return ()

    @classmethod
    def _get_schemesmap(cls):
        schemesmap = {scheme: cls for scheme in cls._schemes()}
        for scls in cls.__subclasses__():
            schemesmap.update(scls._get_schemesmap())
        return schemesmap

    def __new__(
        cls,
        *args,
        schemesmap: dict[str, type["Self"]] = None,
        findclass=False,
        **kwargs,
    ) -> "UriPath":
        if cls is UriPath or findclass:
            uri = Uri(*args, **kwargs)
            cls: type[UriPath] = uri.source.get_scheme_cls(schemesmap)
            if cls is UriPath:
                inst = Uri.__new__(cls, *args, **kwargs)
            else:
                inst = cls.__new__(cls, *args, **kwargs)
            inst._init(uri.source, uri.path, uri.query, uri.fragment, **kwargs)
        else:
            inst = Uri.__new__(cls, *args, **kwargs)
            backend = kwargs.get("backend", None)
            if backend is None:
                for segment in reversed(args):
                    if isinstance(segment, cls):
                        backend = segment.backend
                        break
            inst._backend = backend
        return inst

    def _initbackend(self):
        return None

    def _from_parsed_parts(
        self, source: Source, path: str, query: str, fragment: str, /, **kwargs
    ):
        if "backend" not in kwargs:
            kwargs["backend"] = self.backend
        return super()._from_parsed_parts(source, path, query, fragment, **kwargs)

    def _init(
        self,
        source: Source,
        path: str,
        query: str,
        fragment: str,
        /,
        backend=None,
        **kwargs,
    ):
        if backend is not None:
            self._backend = backend
        super()._init(source, path, query, fragment, **kwargs)

    @property
    def backend(self):
        if self._backend is None:
            self._backend = self._initbackend()
        return self._backend

    def with_backend(self, backend):
        return self._from_parsed_parts(*self.parts, backend=backend)

    def __truediv__(self, key: str | Uri | os.PathLike):
        try:
            return type(self)(self, key, findclass=True)
        except (TypeError, NotImplementedError):
            return NotImplemented

    def with_source(self, source: Source):
        cls = type(self)
        if not source:
            inst = Uri.__new__(UriPath)
        elif source.scheme not in cls._schemes():
            inst = cls.__new__(cls, source.scheme + ":", findclass=True)
        else:
            inst = cls.__new__(cls, backend=self._backend)
        inst._init(source, self.path, self.query, self.fragment)
        return inst

    @_utils.notimplemented
    def _listdir(self) -> "_ty.Iterator[str]": ...

    def _scandir(self) -> "_ty.Iterator[_ty.Tuple[str, _ty.Optional[FileStat]]]":
        """Default: derives from `_listdir()` + one `stat()` per child (no
        round-trip savings over the old `iterdir()`). Schemes whose listing
        call already returns metadata should override this directly instead
        (see docs/guides/extending.md) -- HttpPath/DavPath/SftpPath/FtpPath/
        S3Path all do."""
        for name in self._listdir():
            child = self._make_child_relpath(name)
            try:
                stat = FileStat.from_path(child)
            except OSError:
                stat = None
            yield name, stat

    def _make_child_relpath(self, name: str, stat_hint: "FileStat" = None, **kwargs) -> _ty.Self:
        inst = super()._make_child_relpath(name, backend=self.backend, **kwargs)
        inst._stat_hint = stat_hint
        return inst

    def _pop_stat_hint(self) -> "FileStat | None":
        """Consume this instance's pre-seeded stat (from `_scandir()`), if
        any -- single-use: the next `stat()` call on this same object always
        re-fetches, so a live mutation is never masked by a stale hint."""
        hint = self._stat_hint
        self._stat_hint = None
        return hint

    def iterdir(self) -> "_ty.Iterator[Self]":
        for name, stat in self._scandir():
            yield self._make_child_relpath(name, stat_hint=stat)


_ROOT = Uri("/")
