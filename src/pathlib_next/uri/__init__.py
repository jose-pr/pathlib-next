import os
import typing as _ty
import uritools
from ..fspath import PosixPathname


if _ty.TYPE_CHECKING:
    from typing import Self

from .. import utils as _utils
from .query import Query
from .source import Source

from ..path import Path, Pathname

UriLike: _ty.TypeAlias = "str | Uri | os.PathLike"

_NOSOURCE = Source(None, None, None, None)

_U = _ty.TypeVar("_U", bound="Uri")


class _UriPathParents(_ty.Sequence[_U]):
    """This object provides sequence-like access to the logical ancestors
    of a path.  Don't try to construct it yourself."""

    __slots__ = ("_path", "_segments")

    def __init__(self, path: _U):
        self._path = path
        segments = path.segments
        while segments and segments[-1] == "":
            segments = segments[:-1]
        self._segments = segments

    def __len__(self):
        return len(self._segments)

    @_ty.overload
    def __getitem__(self, idx: slice) -> tuple[_U]: ...
    @_ty.overload
    def __getitem__(self, idx: int) -> _U: ...
    def __getitem__(self, idx: int | slice) -> tuple[_U] | _U:
        if isinstance(idx, slice):
            return tuple(self[i] for i in range(*idx.indices(len(self))))

        if idx >= len(self) or idx < -len(self):
            raise IndexError(idx)
        if idx < 0:
            idx += len(self)
        return self._path.with_path("/".join(self._segments[: -idx - 1]))

    def __repr__(self):
        return "<{}.parents>".format(type(self._path).__name__)


class Uri(Pathname):

    __slots__ = (
        "_raw_uris",
        "_source",
        "_path",
        "_posixpath",
        "_query",
        "_fragment",
        "_uri",
        "_initiated",
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
                continue
            if isinstance(uri, Uri):
                _uris.append(uri)
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
                _uris.append(f"file:{uritools.uriencode(path).decode()}")
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
                src, path, query, fragment = (
                    _uri.parts if isinstance(_uri, Uri) else self._parse_uri(_uri)
                )
                if src:
                    source = src
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

        self._init(source, _path, query, fragment)

    def _init(self, source: Source, path: str, query: str, fragment: str, **kwargs):
        if self._initiated:
            pass
            # raise Exception(f"Uri._init should only be called once")
        self._initiated = True
        self._source = source
        self._path = path
        self._query = query
        self._fragment = fragment

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
            if not self.source.host:
                return self.path
            else:
                return (
                    f"//{self.source.host}/{self.path.removeprefix('/')}"
                    if os.name == "nt"
                    else self.as_posix()
                )
        raise NotImplementedError(f"fspath for {self.source.scheme}")

    def __repr__(self):
        return "{}({!r})".format(type(self).__name__, str(self))

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

    @property
    def posixpath(self) -> PosixPathname:
        if self._posixpath is None:
            self._posixpath = PosixPathname(self.path)
        return self._posixpath

    @property
    def name(self):
        return self.posixpath.name

    @property
    def suffix(self):
        return self.posixpath.suffix

    @property
    def suffixes(self):
        return self.posixpath.suffixes

    @property
    def stem(self):
        return self.posixpath.stem

    def _make_child_relpath(self, name: str, **kwargs) -> _ty.Self:
        cls = type(self)
        inst = cls.__new__(cls)
        inst._init(
            self.source, f"{self.path}/{name}" if self.path else name, "", "", **kwargs
        )
        return inst

    def with_source(self, source: Source):
        return self._from_parsed_parts(source, self.path, self.query, self.fragment)

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

    def with_name(self, name: str):
        return self._from_parsed_parts(
            self.source,
            self.posixpath.with_name(name).as_posix(),
            self.query,
            self.fragment,
        )

    def with_suffix(self, suffix: str):
        return self._from_parsed_parts(
            self.source,
            self.posixpath.with_suffix(suffix).as_posix(),
            self.query,
            self.fragment,
        )

    @property
    def segments(self):
        if not self.path:
            return ()
        return self.path.split("/")

    @property
    def parent(self):
        """The logical parent of the path."""
        segments = self.segments
        if not segments or len(segments) == 2 and segments[1] == "":
            return self
        return self.with_path("/".join(segments[:-1]))

    @property
    def parents(self):
        return _UriPathParents(self)

    def is_absolute(self):
        """True if the path is absolute."""
        return bool(self.source) and self.posixpath.is_absolute()

    def is_relative_to(self, other: UriLike):
        """Return True if the path is relative to another path or False."""
        other = other if isinstance(other, Uri) else Uri(self, _ROOT, other)
        return other == self or other in self.parents

    def relative_to(self, other: UriLike):
        other = other if isinstance(other, Uri) else Uri(other)
        if (self.source and other.source) and other.source != self.source:
            raise ValueError(f"{str(self)!r} is not in the subpath of {str(other)!r}")
        try:
            relpath = self.posixpath.relative_to(other.path)
        except ValueError:
            relpath = self.posixpath
        return self._from_parsed_parts(
            _NOSOURCE, relpath.as_posix(), self.query, self.fragment
        )

    def is_local(self):
        return self.source.is_local()

    def __eq__(self, other: Pathname | str):
        uri = other.as_uri() if isinstance(other, Pathname) else other
        return self.as_uri() == uri

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
    __slots__ = ("_backend",)
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
        return inst

    def _initbackend(self):
        return None

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

    def _load_parts(self):
        super()._load_parts()
        if self.source and self.source.scheme:
            for uri in reversed(self._raw_uris):
                if (
                    isinstance(uri, UriPath)
                    and uri.source.scheme == self.source.scheme
                    and uri._backend
                ):
                    self._backend = uri._backend
                    break

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
            inst = UriPath.__new__(UriPath, source.scheme + ":")
        else:
            inst = cls.__new__(cls, backend=self._backend)
        inst._init(source, self.path, self.query, self.fragment)
        return inst

    @_utils.notimplemented
    def _listdir(self) -> "_ty.Iterator[str]": ...

    def _make_child_relpath(self, name: str, **kwargs) -> _ty.Self:
        inst = super()._make_child_relpath(name, backend=self.backend, **kwargs)
        return inst

    def iterdir(self) -> "_ty.Iterator[Self]":
        for path in self._listdir():
            yield self._make_child_relpath(path)


_ROOT = Uri("/")
