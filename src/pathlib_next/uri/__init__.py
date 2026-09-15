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
from ..path import Path, Pathname, _name_stem, _name_suffix
from ..utils.stat import FileStat
from .query import Query
from .source import (
    _ERRORS,
    Source,
    _compose_uri,
    _decode_host,
    _remove_dot_segments,
    _split_authority,
)

UriLike: TypeAlias = "str | Uri | os.PathLike"

_NOSOURCE = Source(None, None, None, None)


def _authority_key(source: Source) -> tuple:
    scheme, userinfo, host, port = source
    return (
        scheme.lower() if scheme else None,
        userinfo or None,
        str(host).lower() if host else None,
        port or None,
    )


def _same_authority(a: Source, b: Source) -> bool:
    """Whether two sources name the same endpoint (scheme, userinfo, host,
    port), treating `""` and `None` alike. The test for whether a backend,
    connection or rename may cross from one path to another."""
    return _authority_key(a) == _authority_key(b)


_U = _ty.TypeVar("_U", bound="Uri")


def _segments_of(path: str) -> list[str]:
    """Split a normalized posix-style path into segments for prefix
    comparison. "/" alone must yield [""] (the root), not ["", ""]
    (str.split's artifact for a string ending in "/")."""
    if path == "/":
        return [""]
    return path.split("/")


def _uriencode(text: str, safe=""):
    return uritools.uriencode(text, safe=safe, errors=_ERRORS).decode()


def _path_reference(posix: str) -> str:
    """Encode a posix path as a URI reference that parses back to the same
    path. A path starting with "//" (POSIX keeps exactly two leading
    slashes) would otherwise parse as a network-path reference whose first
    segment is a host; "/." in front of it is removed again as a dot
    segment (RFC 3986 5.2.4)."""
    encoded = _uriencode(posix, safe="/")
    return "/." + encoded if encoded.startswith("//") else encoded


class _RelativeLocalPath(str):
    """The encoded posix spelling of a *relative* concrete local path
    (`pathlib.Path("a/b")`, `LocalPath("a/b")`) in `Uri._raw_uris`.

    It joins like a relative `PurePath` -- onto the preceding segments,
    keeping their source -- and supplies a `file:` scheme only when no
    segment has a source at all. Turning it into `file:a/b` up front let
    that scheme replace the base's, so
    `UriPath("sftp://h/srv/") / pathlib.Path("etc/x")` became the LOCAL
    file `/srv/etc/x`."""

    __slots__ = ()


_FILE_SOURCE = Source("file", None, "", None)

#: scheme -> the `importlib.metadata.entry_points` that found no plugin for it.
_ENTRY_POINT_MISSES: "dict[str, object]" = {}


def _is_drive(segment: str) -> bool:
    return len(segment) == 2 and segment[1] == ":" and segment[0].isalpha()


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

    #: Set `True` on a subclass whose `.path` is a filesystem path on the
    #: URI's own host (e.g. `sftp:`) -- lets `__fspath__`/`host_fspath()`
    #: return it for building a command line that runs *on that host*.
    #: Unset (`False`) means `.path` has no such meaning (`http:`, `s3:`, ...).
    _host_filesystem_path = False

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
                    # as_uri() raises ValueError for a relative path, which
                    # joins like a relative PurePath (see _RelativeLocalPath).
                    uri = _RelativeLocalPath(_uriencode(uri.as_posix(), safe="/"))
                _uris.append(uri)
            elif isinstance(uri, (_pathlib.PurePath, Pathname)):
                _uris.append(_path_reference(uri.as_posix()))
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
                _uris.append(_path_reference(posix))
        self._raw_uris = _uris

    @classmethod
    def _parse_uri(cls, uri: str) -> tuple[Source, str, Query, str]:
        # One-pass component extraction: urisplit()'s
        # raw SplitResult fields used directly, authority split once via
        # _split_authority() instead of three independent get*() calls
        # each re-parsing it from scratch. Semantics are provably
        # identical to the getters this replaces (ported logic, fuzzed
        # against uritools as oracle) -- see source.py's helpers for the
        # equivalence notes, including one uritools quirk reproduced
        # on purpose.
        #
        # Deliberate differences from uritools' getters:
        # * the query is NOT decoded: `.query` is the received,
        #   percent-encoded string, sent back out unchanged and decoded only
        #   per key/value by `Query.decode()`/`to_dict()`. Decoding it here
        #   made an escaped `&`, `=` or `+` in a value indistinguishable from
        #   a delimiter.
        # * escapes that are not UTF-8 decode with `surrogateescape` (see
        #   `source._ERRORS`) instead of raising UnicodeDecodeError.
        # * a `data:` payload (RFC 2397) is an opaque octet string, so it
        #   gets no dot-segment removal: "data:,a/./b" is the bytes "a/./b".
        scheme, authority, path, query, fragment = uritools.urisplit(uri)
        scheme = scheme.lower() if scheme is not None else None
        userinfo, host, port = _split_authority(authority)
        if userinfo is not None:
            userinfo = uritools.uridecode(userinfo, errors=_ERRORS)
        host = _decode_host(host) if host is not None else ""
        if scheme != "data":
            path = _remove_dot_segments(path)
        path = uritools.uridecode(path, errors=_ERRORS)
        if fragment is not None:
            fragment = uritools.uridecode(fragment, errors=_ERRORS)
        return (
            Source(scheme, userinfo, host, port),
            path,
            Query(query or ""),
            fragment or "",
        )

    @property
    def parts(self):
        """The tuple of URI components: (source, path, query, fragment)."""
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
        local = False

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
                local = local or isinstance(_uri, _RelativeLocalPath)
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

            if local and not source:
                # Nothing but relative local paths and sourceless strings:
                # still a local file.
                source = _FILE_SOURCE

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
        #
        # `_initiated` is set LAST: the properties return the raw slots as
        # soon as it is truthy, so a thread reading a lazily parsed Uri
        # during another thread's first parse saw `path`/`source` as None.
        self._source = source
        self._path = path
        self._query = query
        self._fragment = fragment
        self._segments_cache = None
        self._suffix_cache = None
        self._stem_cache = None
        self._initiated = True

    def _from_parsed_parts(
        self, source: Source, path: str, query: str, fragment: str, /, **kwargs
    ):
        cls = type(self)
        uri = cls.__new__(cls)
        uri._init(source, path, query, fragment, **kwargs)
        return uri

    def _from_decoded_path(self, path: str, /, **kwargs) -> "_ty.Self":
        """Build a same-type URI whose `.path` is `path` verbatim.

        `path` is an **already-decoded path string**, not URI syntax: `?`,
        `#`, `%` and `:` are ordinary filename characters here. Only
        path-level normalization (dot segments, exactly what `_parse_uri`
        applies *after* decoding) is performed -- nothing is split off and
        nothing is percent-decoded.

        This is what a destination/target argument must go through.
        Feeding such a string back into the URI parser (`Uri(path)`,
        `type(self)(path)`) reads it as syntax: "a?b.txt" silently became
        "a" plus a query, "a#b.txt" became "a" plus a fragment,
        "a%20b.txt" became "a b.txt", and a relative "C:/Temp/x" became
        scheme "c" plus "/Temp/x" -- so the wire call went to a different
        file than the caller named, with no error. Percent-encoding the
        string before parsing would fix the truncation but re-encode an
        already-encoded name (a literal "%20" would come back as a space),
        so the parse is bypassed instead of being fed encoded input.
        """
        return self._from_parsed_parts(
            _NOSOURCE, _remove_dot_segments(path), None, None, **kwargs
        )

    def _rename_target(self, target: UriLike) -> "Uri":
        """Normalize a `rename()`/`replace()` destination to a `Uri`.

        A `str` is an already-decoded path (see `_from_decoded_path`), and
        a relative one is resolved against `self.parent` -- the documented
        sibling-rename semantics ("rename this to a new name in the same
        directory"), not against `self` itself. A `Uri` (of any scheme
        class) is taken as given; anything else keeps the pre-existing
        `Uri(...)` conversion, which is already lossless for
        `PurePath`/`os.PathLike` (they are percent-encoded on the way in
        and decoded back out).
        """
        if isinstance(target, Uri):
            result = target
        else:
            if isinstance(target, str):
                target = self._from_decoded_path(target)
            # target is a Uri by now, so this join re-uses `_load_parts`'
            # existing right-to-left semantics without re-parsing anything.
            result = Uri(self.parent, target)
        if not self._same_location(result):
            # Every scheme renames over its own connection or bucket using
            # only `target.path`, so a target elsewhere (another host, bucket,
            # archive or scheme -- including a local path) used to be renamed
            # in the wrong place, silently. NotImplementedError is move()'s
            # signal to fall back to copy + delete.
            raise NotImplementedError(
                f"rename() cannot cross locations: {self} -> {result}"
            )
        return result

    def _same_location(self, other: "Uri") -> bool:
        """Whether `other` lives where a native `rename()` of `self` can
        reach it: the same endpoint (see `_same_authority`), or a sourceless
        path relative to `self`. Schemes whose namespace is narrower than the
        authority (an archive, an Azure container) override this."""
        return not other.source or _same_authority(self.source, other.source)

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
        # Direct string assembly instead of
        # uricompose()'s full re-validation -- source/path/query/fragment
        # here always came from a parse or our own normalized join state,
        # never arbitrary untrusted input. See source.py's _compose_uri
        # for the equivalence notes (fuzzed against uricompose as oracle).
        scheme = source.scheme.lower() if source.scheme else None
        userinfo = source.userinfo or None
        if sanitize and userinfo:
            userinfo = userinfo.split(":", maxsplit=1)[0] or None
        host = source.host if source.host else None
        port = source.port or None
        return _compose_uri(
            scheme, userinfo, host, port, path, query or None, fragment or None
        )

    def __str__(self):
        """Return the string representation of the path. Deliberately
        sanitized (password dropped from userinfo) since `str()` is what
        logging/printing reach for -- this does NOT round-trip a
        credentialed URI. Use `as_uri(sanitize=False)` for the full URI
        including credentials."""
        return self.as_uri(sanitize=True)

    def __fspath__(self):
        if (self.source.scheme or "file") == "file":
            host = self.source.host
            if not host or (isinstance(host, str) and host.lower() == "localhost"):
                path = self.path
                # "file://localhost/C:/x" keeps the "/" before its drive (it
                # must, to render with an authority); the OS path does not.
                if (
                    os.name == "nt"
                    and path.startswith("/")
                    and _is_drive(path[1:].partition("/")[0])
                ):
                    path = path[1:]
                return path
            elif os.name == "nt":
                # A named host is a UNC share even when it is this machine:
                # "file://fileserver/share/x" is "//fileserver/share/x", never
                # "/share/x" on the current drive. No DNS lookup either.
                return f"//{host}/{self.path.removeprefix('/')}"
            elif self.is_local():
                return self.path
            else:
                raise NotImplementedError("OS Support for not local fspath")

        if self._host_filesystem_path:
            return self.path

        raise NotImplementedError(f"fspath for {self.source.scheme}")

    def host_fspath(self) -> str:
        """Return `.path` for any scheme whose path component is a
        filesystem path on the URI's own host (see `_host_filesystem_path`),
        for building a command line that runs *on that host* (e.g. via a
        remote executor). Unlike `__fspath__`, this never falls back to
        treating the path as local -- it raises `NotImplementedError` for
        schemes with no host-filesystem meaning (`http:`, `s3:`, ...)."""
        if self._host_filesystem_path:
            return self.path
        raise NotImplementedError(f"host_fspath for {self.source.scheme}")

    def __repr__(self):
        if self._initiated:
            return "{}({!r})".format(type(self).__name__, str(self))
        else:
            return super().__repr__()

    def as_uri(self, /, sanitize=False):
        if self._uri is None or sanitize:
            path = self.path
            if path.startswith("//") and not self._has_authority():
                # Without an authority a "//" path would render as one
                # ("file:////srv/x" is host "" and path "//srv/x"), which
                # raised ValueError from str()/repr()/hash(). "/." keeps it
                # a path and parses back to the same one.
                path = "/." + path
            uri = self._format_parsed_parts(
                self.source, path, self.query, self.fragment, sanitize=sanitize
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
        """The query exactly as received: still percent-encoded, so an
        escaped `&`, `=` or `+` inside a value survives the round trip. Use
        `Query.decode()`/`to_dict()` for decoded pairs."""
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
        """Return a new URI with the source replaced."""
        return self._from_parsed_parts(source, self.path, self.query, self.fragment)

    def with_segments(self, *segments: str):
        """Return a new URI with the path segments replaced."""
        if not segments:
            return self.with_path("")
        return self.with_path("/".join(segments))

    def with_path(self, path: str | Pathname):
        """Return a new URI with the path replaced."""
        return self._from_parsed_parts(
            self.source,
            path.as_posix() if isinstance(path, Pathname) else path,
            self.query,
            self.fragment,
        )

    def with_query(self, query: str):
        """Return a new URI with the query replaced.

        A `str` is taken in the percent-encoded form `.query` returns and is
        sent as given; a mapping or a sequence of pairs is encoded by
        `Query`."""
        if not isinstance(query, Query):
            query = Query(query)
        return self._from_parsed_parts(self.source, self.path, query, self.fragment)

    def with_fragment(self, fragment: str):
        """Return a new URI with the fragment replaced."""
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
            self._suffix_cache = _name_suffix(self.name)
        return self._suffix_cache

    @property
    def stem(self) -> str:
        if self._stem_cache is None:
            self._stem_cache = _name_stem(self.name)
        return self._stem_cache

    @property
    def parent(self):
        """The logical parent of the path."""
        segments = self.segments
        if not segments or len(segments) == 2 and segments[1] == "":
            return self
        if len(segments) == 2 and segments[0] == "":
            # "/a" -> "/", not "": the empty path is relative, so the parent
            # of a top-level FileUri resolved to the current directory and
            # `parents` never reached the root.
            return self.with_path("/")
        return self.with_path("/".join(segments[:-1]))

    def _has_authority(self) -> bool:
        source = self.source
        return bool(source.host or source.userinfo or source.port)

    def _match_parts(self) -> tuple[bool, list[str]]:
        # An authority with an empty path ("http://h") is that authority's
        # root, the same as "http://h/" (RFC 3986). The host itself is never
        # part of what match() sees.
        anchored, names = super()._match_parts()
        return anchored or (not self.path and self._has_authority()), names

    def _prefix_segments(self) -> list[str]:
        """Segments for `is_relative_to`/`relative_to` prefix comparison:
        `[""]` for a root (including an authority with an empty path, which
        `normpath` used to turn into "."), `[]` for the empty relative path."""
        if not self.path:
            return [""] if self._has_authority() else []
        path = self.normalized_path
        return [] if path == "." else _segments_of(path)

    @property
    def normalized_path(self):
        """Return the normalized path using posixpath rules."""
        if self._normalized_path is None:
            self._normalized_path = _posix.normpath(self.path)
        return self._normalized_path

    def is_absolute(self):
        """True if the path is absolute: it starts with "/", with or without
        a source (as `PurePosixPath("/a")` is absolute)."""
        return self.path.startswith("/")

    def is_relative_to(self, other: UriLike):
        """Return True if the path is relative to another path or False."""
        # Uri(other), NOT Uri(self, _ROOT, other): anchoring a str `other`
        # at self's root turned `Uri("a/b").is_relative_to("a")` into a
        # comparison against "/a" and answered False, disagreeing with the
        # object form of the same call. `relative_to()` below already
        # parsed a str standalone; this matches it and CPython.
        # The same-authority case still works: a standalone parse leaves
        # `other.source` empty, which the guard below treats as compatible
        # with any `self.source`, and the segment prefix compare is
        # unaffected -- `Uri("http://h/a/b").is_relative_to("/a")` is True.
        other = other if isinstance(other, Uri) else Uri(other)
        if not (
            (other.source == self.source)
            or not (bool(self.source) and bool(other.source))
        ):
            return False
        # Segment-wise prefix comparison: a naive startswith() on the raw
        # strings would report "/foo/bar2" as relative to "/foo/bar".
        _other = other._prefix_segments()
        _self = self._prefix_segments()
        if _self[:1] == [""] and _other[:1] != [""]:
            # An absolute path is never relative to a relative one (the
            # empty relative path included), as in pathlib.
            return False
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
        # _segments_of, not raw .segments: the latter gives the URI root
        # ("/") a spurious 2-tuple ("", "") instead of ("",), which used to
        # make relative_to(<root>) drop the child's only real segment
        # (found via property testing, polish_perf/06).
        self_segs = self._prefix_segments()
        path_segs = path._prefix_segments()
        parts = [".."] * step + self_segs[len(path_segs) :]
        return self._from_parsed_parts(
            _NOSOURCE, "/".join(parts), self.query, self.fragment
        )

    def is_local(self):
        """Return True if the URI points to a local resource."""
        return self.source.is_local()

    def __eq__(self, other: Uri | str):
        # Only a Uri or a URI string, both of which hash as their URI text
        # like __hash__. Another Pathname (MemPath, LocalPath) hashes by its
        # own rule, so equal-but-differently-hashed pairs broke sets and
        # dicts, and a relative LocalPath's as_uri() raised out of `==`.
        if isinstance(other, Uri):
            uri = other.as_uri()
        elif isinstance(other, str):
            uri = other
        else:
            return NotImplemented
        return self.as_uri() == uri

    def __hash__(self):
        return hash(self.as_uri())

    def __rtruediv__(self, key: str):
        """`"prefix" / uri`. The str is a decoded path joined in front of
        this one, never URI syntax (so "C:/x" is not read as a scheme)."""
        if not isinstance(key, str):
            return NotImplemented
        # A plain Uri: building the prefix must not touch `self.backend`.
        return type(self)(Uri()._from_decoded_path(key), self)

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
    `docs/guides/extending.md`. Prefer overriding `_scandir()` over `_listdir()` when the
    listing call already returns type/size/mtime metadata (PROPFIND, MLSD,
    `listdir_attr`, an S3 list page, ...) -- `walk()`/`glob()` then answer
    `is_dir()` on the results for free, without a stat request per entry."""

    __slots__ = ("_backend", "_stat_hint", "_backend_origin")
    __SCHEMES: _ty.Sequence[str] = ()
    __SCHEMESMAP: _ty.Mapping[str, type["Self"]] = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # A scheme class defined after the first dispatch must be found by
        # the next one: drop every cached map that could include it.
        for base in cls.__mro__:
            if isinstance(base, type) and issubclass(base, UriPath):
                setattr(base, f"_{base.__name__}__SCHEMESMAP", None)
        _ENTRY_POINT_MISSES.clear()

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
        else:
            _ENTRY_POINT_MISSES.clear()
        schemesmap = cls._get_schemesmap()
        setattr(cls, _propname, schemesmap)
        return schemesmap

    @classmethod
    def _schemes(cls) -> _ty.Sequence[str]:
        try:
            return getattr(cls, f"_{cls.__name__}__SCHEMES")
        except AttributeError:
            return ()

    @classmethod
    def _get_schemesmap(cls):
        schemesmap = {scheme: cls for scheme in cls._schemes()}
        for scls in cls.__subclasses__():
            schemesmap.update(scls._get_schemesmap())
        return schemesmap

    @classmethod
    def _load_entry_point(cls, scheme: str) -> bool:
        """Attempt to load a scheme class from package entry points.

        Looks for entry points in the 'pathlib_next.schemes' group where
        the name matches the requested scheme.
        """
        import importlib.metadata as _metadata

        # A miss is remembered: scanning every installed distribution cost
        # 13-29 ms on each construction with an unregistered scheme
        # (including "C:/x" on Windows, read as scheme "c"). Keyed on the
        # lookup function as well, so a replaced `entry_points` is asked
        # afresh; `_schemesmap(reload=True)` and a new subclass clear it.
        if _ENTRY_POINT_MISSES.get(scheme) is _metadata.entry_points:
            return False
        try:
            eps = _metadata.entry_points(group="pathlib_next.schemes")
        except TypeError:
            # Python 3.9 fallback
            eps = _metadata.entry_points().get("pathlib_next.schemes", ())

        for ep in eps:
            if ep.name == scheme:
                ep.load()
                return True
        _ENTRY_POINT_MISSES[scheme] = _metadata.entry_points
        return False

    @classmethod
    def _load_builtin_scheme(cls, scheme: str) -> bool:
        """Attempt to load a builtin scheme module dynamically on-demand."""
        _BUILTIN_SCHEMES = {
            "file": "pathlib_next.uri.schemes.file",
            "data": "pathlib_next.uri.schemes.data",
            "zip": "pathlib_next.uri.schemes.archive",
            "tar": "pathlib_next.uri.schemes.archive",
            "archive": "pathlib_next.uri.schemes.archive",
            "archive+zip": "pathlib_next.uri.schemes.archive",
            "archive+tar": "pathlib_next.uri.schemes.archive",
            "ftp": "pathlib_next.uri.schemes.ftp",
            "ftps": "pathlib_next.uri.schemes.ftp",
            "http": "pathlib_next.uri.schemes.http",
            "https": "pathlib_next.uri.schemes.http",
            "dav": "pathlib_next.uri.schemes.dav",
            "davs": "pathlib_next.uri.schemes.dav",
            "sftp": "pathlib_next.uri.schemes.sftp",
            "s3": "pathlib_next.uri.schemes.s3",
            "gs": "pathlib_next.uri.schemes.gs",
            "az": "pathlib_next.uri.schemes.az",
            "github": "pathlib_next.uri.schemes.github",
            "gitlab": "pathlib_next.uri.schemes.gitlab",
            "git": "pathlib_next.uri.schemes.git",
            "git+github": "pathlib_next.uri.schemes.git",
            "git+gitlab": "pathlib_next.uri.schemes.git",
        }
        module_name = _BUILTIN_SCHEMES.get(scheme)
        if module_name:
            import importlib

            try:
                importlib.import_module(module_name)
                return True
            except ImportError:
                pass
        return False

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
                        # Checked against the finished path on first use:
                        # `base / "http://other/x"` must not carry `base`'s
                        # session (auth, token) to another host.
                        inst._backend_origin = segment.source
                        break
            inst._backend = backend
        return inst

    def _initbackend(self):
        return None

    def _from_parsed_parts(
        self, source: Source, path: str, query: str, fragment: str, /, **kwargs
    ):
        if "backend" not in kwargs:
            kwargs["backend"] = (
                self.backend
                if not source or _same_authority(source, self.source)
                else None
            )
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
        """The connection or session state backend instance."""
        self._check_inherited_backend()
        if self._backend is None:
            self._backend = self._initbackend()
        return self._backend

    def _coerce_target(self, target: str) -> "UriPath":
        # A str destination to copy()/move() is still URI syntax: that is what
        # makes a cross-scheme `copy("s3://bucket/key")` work. (Tracked: a bare
        # path string therefore has no source.)
        return type(self)(target)

    def _check_inherited_backend(self):
        # A backend copied from a join segment is only valid for the same
        # endpoint; for any other it is dropped and rebuilt from this path.
        origin = self._backend_origin
        if origin is not None:
            self._backend_origin = None
            if self._backend is not None and not _same_authority(origin, self.source):
                self._backend = None

    def with_backend(self, backend):
        """Return a new path instance sharing the same backend state."""
        return self._from_parsed_parts(*self.parts, backend=backend)

    def __truediv__(self, key: str | Uri | os.PathLike):
        # Only converting `key` decides NotImplemented. Catching TypeError
        # around the whole construction turned a bug inside a scheme's
        # __new__/_init into "unsupported operand type(s) for /".
        converted = Uri.__new__(Uri)
        try:
            converted.__init__(key)
        except (TypeError, NotImplementedError):
            return NotImplemented
        return type(self)(self, *converted._raw_uris, findclass=True)

    def __rtruediv__(self, key: str):
        if not isinstance(key, str):
            return NotImplemented
        return type(self)(Uri()._from_decoded_path(key), self, findclass=True)

    def joinpath(self, *args: str | Uri | os.PathLike) -> "UriPath":
        """Combine this path with segments, choosing the result's class from
        its scheme as `/` does: joining an absolute local path gives a
        `FileUri`, not this class carrying a `file:` URI."""
        return type(self)(self, *args, findclass=True)

    def with_source(self, source: Source):
        cls = type(self)
        if not source or not source.scheme:
            # Nothing to dispatch on (`source.scheme + ":"` raised TypeError
            # for a host-only Source): a plain UriPath.
            inst = Uri.__new__(UriPath)
        elif source.scheme not in cls._schemes():
            inst = cls.__new__(cls, source.scheme + ":", findclass=True)
        else:
            self._check_inherited_backend()
            same = _same_authority(source, self.source)
            inst = cls.__new__(cls, backend=self._backend if same else None)
        inst._init(source, self.path, self.query, self.fragment)
        return inst

    def _symlink_target(self, target: "UriLike") -> "_ty.Self":
        """`Path._symlink_target()` for URI-backed paths: a `str` target is
        an already-decoded path, never URI syntax (see
        `Uri._from_decoded_path`).

        The default `type(self)(target)` ran the link target back through
        the URI parser, so `symlink_to("/mnt/cache?v=2")` created a link
        pointing at `/mnt/cache`. Unlike `_rename_target()` this never
        anchors at `self.parent`: a symlink target is stored as given, so
        a relative one stays relative.
        """
        if isinstance(target, str):
            return self._from_decoded_path(target)
        return target

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
                # Non-following, like Path._scandir(): rm(recursive=True) and
                # walk() trust this stat, and a following one reported a
                # directory symlink as a real directory to descend into.
                stat = FileStat.from_path(child, follow_symlink=False)
            except OSError:
                stat = None
            yield name, stat

    def _make_child_relpath(
        self, name: str, stat_hint: "FileStat" = None, **kwargs
    ) -> _ty.Self:
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
