from __future__ import annotations

import functools as _functools
import ipaddress as _ip
import socket as _socket
import typing as _ty

import uritools as _uritools

from .. import utils as _utils

_IPAddress = _ty.Union[_ip.IPv4Address, _ip.IPv6Address]

if _ty.TYPE_CHECKING:
    from . import UriPath

_DIGITS = "0123456789"


def _split_authority(authority: "str | None") -> "tuple[str | None, str | None, int | None]":
    """One-pass split of a raw URI authority into (userinfo, host, port) --
    RAW/undecoded strings, port as int-or-None. Ported directly from
    `uritools.SplitResult`'s `.userinfo`/`.host`/`.port` properties (each
    independently re-`rpartition`s `authority` today, ~3-4x redundant work
    per `Uri()` construction across the getter calls this replaces) rather
    than reinventing the logic -- verified equivalent by fuzzing 20000+
    generated URIs against uritools as the oracle (see uri_parse_perf.md).

    Faithfully reproduces one uritools quirk, not a bug we get to "fix"
    here: `.host` does NOT check whether ':' was actually present in the
    userinfo-stripped remainder -- for a colon-less, all-digit remainder
    (e.g. authority `"0"`), `str.rpartition`'s not-found fallback shape
    (`('', '', s)`) puts the whole string in the *port* slot, and since an
    all-digit string passes the "looks like a port" check, uritools'
    `.host` returns `''` instead of the real host. `.port` (used for the
    `port` result here) doesn't share this quirk -- it explicitly checks
    whether ':' was found via the middle `rpartition` element instead of
    inferring it from digit-ness, so it's correct: no port, not empty."""
    if authority is None:
        return None, None, None
    userinfo, at_sep, hostinfo = authority.rpartition("@")
    if not at_sep:
        userinfo = None
    host_part, _, port_part = hostinfo.rpartition(":")
    if port_part.lstrip(_DIGITS):
        host = hostinfo
    else:
        host = host_part
    _, port_sep, port_str = authority.rpartition(":")
    if port_sep and not port_str.lstrip(_DIGITS):
        port = int(port_str) if port_str else None
    else:
        port = None
    return userinfo, host, port


def _decode_host(host: str) -> "str | _IPAddress":
    """Decode a raw (still percent-encoded, undecoded) host string --
    ported from `uritools.SplitResult.gethost()`'s bracket/IP-literal
    handling (private there). Bracket-mismatch and bare-`v`-prefixed
    IP-literal-version rejection match uritools' actual behavior exactly
    (including its case-sensitive-only `v` check, despite RFC 3986
    describing it as case-insensitive -- verified by fuzzing, don't
    "correct" this without checking uritools itself doesn't diverge)."""
    if host.startswith("[") and host.endswith("]"):
        literal = host[1:-1]
        if literal.startswith("v"):
            raise ValueError("address mechanism not supported")
        return _ip.IPv6Address(literal)
    if host.startswith("[") or host.endswith("]"):
        raise ValueError(f"Invalid host {host!r}: mismatched brackets")
    try:
        return _ip.IPv4Address(host)
    except ValueError:
        return _uritools.uridecode(host).lower()


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 5.2.4 dot-segment removal on a raw (still percent-encoded)
    path -- ported from `uritools.SplitResult.getpath()`'s private
    `__remove_dot_segments` helper, applied BEFORE percent-decoding (a
    percent-encoded `%2e` must not be treated as a literal `.` segment,
    matching uritools' own ordering)."""
    pseg = []
    for s in path.split("/"):
        if s == ".":
            continue
        elif s != "..":
            pseg.append(s)
        elif len(pseg) == 1 and not pseg[0]:
            continue
        elif pseg and pseg[-1] != "..":
            pseg.pop()
        else:
            pseg.append(s)
    if path.rpartition("/")[2] in (".", ".."):
        pseg.append("")
    if path and len(pseg) == 1 and pseg[0] == "":
        pseg.insert(0, ".")
    return "/".join(pseg)


# --- composer (uri_parse_perf.md Phase 2) -----------------------------
# `uritools.uricompose()` re-validates every component on every call
# (scheme regex, authority-string re-parsing, IP-literal detection on
# plain strings, ...) -- necessary for its own "arbitrary input" contract,
# wasted work when composing FROM already-canonical parsed/normalized
# state (a `Source`/path/query/fragment that came from `_parse_uri` or
# `Uri`'s own join logic). These helpers do direct string assembly,
# reusing uritools' own `uriencode()` for percent-encoding (kept, not
# reimplemented -- same reasoning as the parse side) and replicating only
# the transformations that affect the OUTPUT STRING (lowercasing,
# IP-literal bracketing, the colon-in-first-segment "./" escape), not the
# validation that only matters for genuinely untrusted input (scheme
# regex, "path must start with a leading '/' with an authority present"
# -- `Uri._load_parts()` already enforces that invariant on construction,
# it can never actually fire here for a real `Uri`; kept anyway below
# since it's a single free `.startswith()` check). Verified equivalent to
# `uricompose()` (via the composer's actual call site,
# `_format_parsed_parts`) by fuzzing 30000+ generated
# (scheme, userinfo, host, port, path, query, fragment) combinations.

_SUB_DELIMS = "!$&'()*+,;="
_SAFE_USERINFO = _SUB_DELIMS + ":"
_SAFE_HOST = _SUB_DELIMS
_SAFE_PATH = _SUB_DELIMS + ":@/"
_SAFE_QUERY = _SUB_DELIMS + ":@/?"
_SAFE_FRAGMENT = _SAFE_QUERY


def _compose_host(host: "str | _IPAddress") -> str:
    """Encode a host for composition -- mirrors `uritools`' private
    `_authority()`/`_host()` composer helpers, including the fact that a
    bare (non-bracketed) string that happens to parse as IPv6 gets
    auto-bracketed (matches `uricompose`'s own behavior for a manually
    -constructed `Source(..., host="::1", ...)`, not just an
    already-bracketed literal)."""
    if isinstance(host, _ip.IPv6Address):
        return f"[{host.compressed}]"
    if isinstance(host, _ip.IPv4Address):
        return host.compressed
    if host.startswith("[") and host.endswith("]"):
        return f"[{_ip.IPv6Address(host[1:-1]).compressed}]"
    try:
        return f"[{_ip.IPv6Address(host).compressed}]"
    except ValueError:
        return _uritools.uriencode(host.lower(), _SAFE_HOST).decode()


def _compose_uri(
    scheme: "str | None",
    userinfo: "str | None",
    host: "str | _IPAddress | None",
    port: "int | None",
    path: str,
    query: "str | None",
    fragment: "str | None",
) -> str:
    parts = []
    if scheme is not None:
        parts.append(scheme)
        parts.append(":")
    has_authority = userinfo is not None or host is not None or port is not None
    if has_authority:
        parts.append("//")
        if userinfo is not None:
            parts.append(_uritools.uriencode(userinfo, _SAFE_USERINFO).decode())
            parts.append("@")
        if host is not None:
            parts.append(_compose_host(host))
        if port is not None:
            parts.append(":")
            parts.append(str(port))
    path_enc = _uritools.uriencode(path, _SAFE_PATH).decode() if path else ""
    if has_authority and path_enc and not path_enc.startswith("/"):
        raise ValueError("Invalid path with authority component")
    if not has_authority and path_enc.startswith("//"):
        raise ValueError("Invalid path without authority component")
    if scheme is None and not has_authority and not path_enc.startswith("/"):
        if ":" in path_enc.partition("/")[0]:
            path_enc = "./" + path_enc
    parts.append(path_enc)
    if query is not None:
        parts.append("?")
        parts.append(_uritools.uriencode(query, _SAFE_QUERY).decode())
    if fragment is not None:
        parts.append("#")
        parts.append(_uritools.uriencode(fragment, _SAFE_FRAGMENT).decode())
    return "".join(parts)


class Source(_ty.NamedTuple):
    """A URI's scheme/userinfo/host/port -- everything before the path.
    Falsy (`bool(source) is False`) when every field is empty/None."""

    scheme: str | None
    userinfo: str | None
    host: str | _IPAddress | None
    port: int | None

    def __bool__(self):
        return (
            (self[0] != "" and self[0] is not None)
            or (self[1] != "" and self[1] is not None)
            or (self[2] != "" and self[2] is not None)
            or (self[3] != "" and self[3] is not None)
        )

    def __str__(self) -> str:
        return _uritools.uricompose(
            scheme=self.scheme, userinfo=self.userinfo, host=self.host, port=self.port
        )

    @classmethod
    def from_str(cls, source: str, strict=True):
        uri = _uritools.urisplit(source)
        if strict and (uri.path or uri.fragment or uri.query):
            raise ValueError(source)
        scheme = uri.scheme.lower() if uri.scheme is not None else None
        userinfo, host, port = _split_authority(uri.authority)
        if userinfo is not None:
            userinfo = _uritools.uridecode(userinfo)
        if host is not None:
            host = _decode_host(host)
        return cls(scheme, userinfo, host, port)

    def keys(self):
        return self._asdict().keys()

    def __getitem__(self, key: int | str):
        if not isinstance(key, str):
            key = self._fields[key]
        return getattr(self, key)

    def parsed_userinfo(self):
        parts = []
        if self.userinfo:
            parts = self.userinfo.split(":", maxsplit=1)
        parts = parts + ["", ""]
        return parts[0], parts[1]

    def get_scheme_cls(self, schemesmap: _ty.Mapping[str, type["UriPath"]] = None):
        from . import UriPath

        if self.scheme:
            if schemesmap is None:
                schemesmap = UriPath._schemesmap()
            _cls = schemesmap.get(self.scheme, None)
            if _cls is None:
                if UriPath._load_entry_point(self.scheme) or UriPath._load_builtin_scheme(self.scheme):
                    schemesmap = UriPath._schemesmap(reload=True)
                    _cls = schemesmap.get(self.scheme, None)
            return _cls if _cls else UriPath
        return UriPath

    @_functools.lru_cache(maxsize=256)
    def is_local(self):
        """Whether `host` resolves to this machine.

        Caches per unique Source (Source is an immutable value type), since
        this does a DNS lookup (socket.gethostbyname) -- never call it on a
        hot path uncached.
        """
        host = self.host
        if not host or host == "localhost":
            return True
        if isinstance(host, str):
            host = _ip.ip_address(_socket.gethostbyname(host))
        return host.is_loopback or host in _utils.get_machine_ips()


_NOSOURCE = Source(None, None, None, None)
