from __future__ import annotations

import ipaddress as _ip
import re as _re
import threading as _threading
import time as _time
import typing as _ty

import uritools as _uritools

_IPAddress = _ty.Union[_ip.IPv4Address, _ip.IPv6Address]

if _ty.TYPE_CHECKING:
    from . import UriPath

_DIGITS = "0123456789"

#: Decoding and encoding error handler for percent-escapes. A valid URI may
#: escape bytes that are not UTF-8 (`caf%E9.html` from a latin-1 server);
#: `surrogateescape` carries each such byte through the decoded `str` and
#: back out to the same escape, where the default `strict` raised
#: UnicodeDecodeError while the path object was being constructed.
_ERRORS = "surrogateescape"


class _Userinfo(str):
    """A decoded userinfo that remembers which colon separated the user name
    from the password. It equals, hashes and prints as the plain
    `user:password` text, so code that treats `Source.userinfo` as a `str`
    is unaffected; only the composer and `Source.parsed_userinfo()` read
    the split. Without it a user name holding an escaped colon (`us%3Aer`)
    could not be told from a password separator once decoded."""

    __slots__ = ("user", "password")

    def __new__(cls, user: str, password: "str | None" = None):
        obj = str.__new__(cls, user if password is None else f"{user}:{password}")
        obj.user = user
        obj.password = password
        return obj

    def __reduce__(self):
        return _Userinfo, (self.user, self.password)


def _split_userinfo_text(userinfo: str) -> "tuple[str, str | None]":
    """`(user, password)` of a userinfo: the split `_Userinfo` recorded, else
    at the first colon. `password` is None when there is no colon."""
    if isinstance(userinfo, _Userinfo):
        return userinfo.user, userinfo.password
    user, colon, password = userinfo.partition(":")
    return user, (password if colon else None)


def _decode_userinfo(raw: str) -> _Userinfo:
    """Decode a raw userinfo: split at the first literal colon FIRST, then
    decode each half, so an escaped `%3A` stays part of the name."""
    user, colon, password = raw.partition(":")
    return _Userinfo(
        _uritools.uridecode(user, errors=_ERRORS),
        _uritools.uridecode(password, errors=_ERRORS) if colon else None,
    )


def _parse_port(text: str) -> "int | None":
    """The port a run of ASCII digits names (None for no digits), which must
    be 0-65535 (RFC 3986 3.2.3 leaves it open; TCP and UDP do not). The
    message names no part of the input: it may sit beside a password."""
    if not text:
        return None
    digits = text.lstrip("0") or "0"
    if len(digits) > 5 or int(digits) > 65535:
        raise ValueError("port out of range 0-65535")
    return int(digits)


def _split_authority(
    authority: "str | None",
) -> "tuple[str | None, str | None, int | None]":
    """One-pass split of a raw URI authority into (userinfo, host, port) --
    RAW/undecoded strings, port as int-or-None. Ported from
    `uritools.SplitResult`'s `.userinfo`/`.host`/`.port` properties (each
    independently re-`rpartition`s `authority`, ~3-4x redundant work per
    `Uri()` construction) -- verified equivalent by fuzzing against uritools
    as the oracle (tests/test_properties.py), with the differences listed
    there:

    * a host is followed by a port only when a ':' was found, so a host of
      digits alone (`s3://20240101/key`) stays the host; uritools reads it
      as a port and returns an empty host;
    * the port is 0-65535; a ':' followed by anything else that is not a
      bracketed address is an invalid port (`ValueError`), not part of the
      host name."""
    if authority is None:
        return None, None, None
    userinfo, at_sep, hostinfo = authority.rpartition("@")
    if not at_sep:
        userinfo = None
    host, colon, tail = hostinfo.rpartition(":")
    if colon and not tail.lstrip(_DIGITS):
        return userinfo, host, _parse_port(tail)
    if colon and not hostinfo.startswith("["):
        raise ValueError("invalid port: it must be digits")
    return userinfo, hostinfo, None


def _ipv6_from_literal(literal: str) -> _ip.IPv6Address:
    """The address a bracketed IPv6 literal names. A zone is introduced by
    `%25` (RFC 6874: the `%` of `address%zone` is escaped inside a URI) and
    is percent-decoded; a bare `%` is read as the delimiter as well, as
    `ipaddress` does."""
    address, sep, zone = literal.partition("%25")
    if sep:
        literal = f"{address}%{_uritools.uridecode(zone, errors=_ERRORS)}"
    return _ip.IPv6Address(literal)


def _decode_host(host: str) -> "str | _IPAddress":
    """Decode a raw (still percent-encoded, undecoded) host string --
    ported from `uritools.SplitResult.gethost()`'s bracket/IP-literal
    handling (private there). Bracket-mismatch and bare-`v`-prefixed
    IP-literal-version rejection match uritools' actual behavior exactly
    (including its case-sensitive-only `v` check, despite RFC 3986
    describing it as case-insensitive -- verified by fuzzing, don't
    "correct" this without checking uritools itself doesn't diverge).
    An IPv6 zone is read as RFC 6874 says (`_ipv6_from_literal`)."""
    if host.startswith("[") and host.endswith("]"):
        literal = host[1:-1]
        if literal.startswith("v"):
            raise ValueError("address mechanism not supported")
        return _ipv6_from_literal(literal)
    if host.startswith("[") or host.endswith("]"):
        raise ValueError(f"Invalid host {host!r}: mismatched brackets")
    try:
        return _ip.IPv4Address(host)
    except ValueError:
        return _uritools.uridecode(host, errors=_ERRORS).lower()


def _parse_source(scheme: "str | None", authority: "str | None") -> "Source":
    """The `Source` of an already split URI: `scheme` is lower-cased by the
    caller, `authority` is the raw text. Used by `Uri` and `Source.from_str`
    so both read an authority the same way."""
    userinfo, host, port = _split_authority(authority)
    if userinfo is not None:
        userinfo = _decode_userinfo(userinfo)
    host = _decode_host(host) if host is not None else ""
    return Source(scheme, userinfo, host, port)


def _is_drive(segment: str) -> bool:
    """Whether a path segment is a Windows drive ("C:")."""
    return len(segment) == 2 and segment[1] == ":" and segment[0].isalpha()


def _remove_dot_segments(path: str, *, drive: bool = False) -> str:
    """RFC 3986 5.2.4 dot-segment removal on a path -- ported from
    `uritools.SplitResult.getpath()`'s private `__remove_dot_segments`
    helper. Apply it to a raw (still percent-encoded) path before decoding,
    and again to the decoded path: a `%2e` only becomes a `.` segment
    once decoded. A `..` that would pass the root of an absolute path is
    dropped; a relative path keeps its leading `..`.

    `drive=True` (a `file:` path on Windows) makes a leading drive segment
    ("C:/x", "/C:/x") the anchor, which `..` never climbs above, as for
    `PureWindowsPath`."""
    if drive:
        head = path[1:] if path.startswith("/") else path
        letter, sep, rest = head.partition("/")
        if sep and _is_drive(letter):
            lead = path[: len(path) - len(head)]
            return lead + letter + _remove_dot_segments("/" + rest)
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


# --- composer (direct-assembly fast path) ----------------------------
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
_SAFE_USER = _SUB_DELIMS
_SAFE_USERINFO = _SUB_DELIMS + ":"
_SAFE_HOST = _SUB_DELIMS
_SAFE_PATH = _SUB_DELIMS + ":@/"
_SAFE_QUERY = _SUB_DELIMS + ":@/?"
_SAFE_FRAGMENT = _SAFE_QUERY


_PERCENT_ESCAPE = _re.compile("(%[0-9A-Fa-f]{2})")
_SCHEME_RE = _re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*")


def _lenient_utf8(text: str) -> bytes:
    """`text` as UTF-8 where a surrogate that `surrogateescape` cannot carry
    (a lone U+D800-U+DBFF or U+DC00-U+DC7F, which an NTFS name may hold) is
    written the way `surrogatepass` writes it, and an escaped byte
    (U+DC80-U+DCFF) is that byte again."""
    out = bytearray()
    for char in text:
        code = ord(char)
        if 0xDC80 <= code <= 0xDCFF:
            out.append(code - 0xDC00)
        else:
            out += char.encode("utf-8", "surrogatepass")
    return bytes(out)


def _encode(text: str, safe: str) -> str:
    """`text` percent-encoded as UTF-8, keeping the characters of `safe`.
    A decoded path may hold escaped bytes (`_ERRORS`) and, from a Windows
    listing, a lone surrogate; neither may make the composer raise."""
    try:
        return _uritools.uriencode(text, safe, errors=_ERRORS).decode()
    except UnicodeEncodeError:
        return _uritools.uriencode(_lenient_utf8(text), safe).decode()


def _encode_userinfo(userinfo: str) -> str:
    """A userinfo for composition: the user name escapes its colons, the
    password keeps them (RFC 3986 3.2.1)."""
    user, password = _split_userinfo_text(userinfo)
    text = _encode(user, _SAFE_USER)
    if password is not None:
        text += ":" + _encode(password, _SAFE_USERINFO)
    return text


def _encode_raw_query(query: str) -> str:
    """Emit a stored query for the wire: it is kept in its received,
    still-percent-encoded form (see `Uri.query`), so an existing `%XX`
    escape and every delimiter (`&`, `=`, `+`, `;`) go out unchanged. Only
    characters that cannot appear in a query at all (a space, non-ASCII, a
    `%` that starts no escape) are encoded.

    Decoding at parse time and re-encoding here with `&`/`=`/`+` as safe
    characters turned `name=a%26b&sig=ab%2Bcd%3D%3D` into
    `name=a&b&sig=ab+cd==` on the wire, corrupting signed URLs."""
    pieces = _PERCENT_ESCAPE.split(query)
    for index in range(0, len(pieces), 2):
        if pieces[index]:
            pieces[index] = _encode(pieces[index], _SAFE_QUERY)
    return "".join(pieces)


def _compose_ipv6(address: _ip.IPv6Address) -> str:
    """A bracketed IPv6 literal; a zone is written after `%25` and
    percent-encoded (RFC 6874)."""
    text, sep, zone = address.compressed.partition("%")
    if sep:
        text = f"{text}%25{_encode(zone, '')}"
    return f"[{text}]"


def _compose_host(host: "str | _IPAddress") -> str:
    """Encode a host for composition -- mirrors `uritools`' private
    `_authority()`/`_host()` composer helpers, including the fact that a
    bare (non-bracketed) string that happens to parse as IPv6 gets
    auto-bracketed (matches `uricompose`'s own behavior for a manually
    -constructed `Source(..., host="::1", ...)`, not just an
    already-bracketed literal). Deliberately NOT replaced by netimps'
    `join_host`/`FQDN`: those implement the RFC reading, this function the
    uritools one this module's output must round-trip through (and `FQDN`
    would reject names this composes), except where noted: an IPv6 zone is
    written as RFC 6874 says."""
    if isinstance(host, _ip.IPv6Address):
        return _compose_ipv6(host)
    if isinstance(host, _ip.IPv4Address):
        return host.compressed
    if host.startswith("[") and host.endswith("]"):
        return _compose_ipv6(_ipv6_from_literal(host[1:-1]))
    try:
        return _compose_ipv6(_ip.IPv6Address(host))
    except ValueError:
        pass
    host = host.lower()
    if not host.isascii():
        # RFC 3986 3.2.2: a non-ASCII registered name meant for DNS should be
        # produced in its IDNA form, not percent-encoded. HTTP clients
        # IDNA-encode only a non-ASCII host, so "b%c3%bccher.example" was
        # sent verbatim and never resolved. A name the codec rejects (an
        # empty or over-long label, a surrogate-escaped byte) keeps the
        # percent-encoded form.
        try:
            return _idna_encode(host)
        except UnicodeError:
            pass
    return _encode(host, _SAFE_HOST)


def _idna_encode(host: str) -> str:
    """`host` in IDNA (ASCII) form: IDNA 2008 with UTS #46 mapping through
    the `idna` package when installed (what `requests` itself uses), else
    the stdlib IDNA 2003 codec. Raises UnicodeError for an invalid name."""
    try:
        import idna as _idna
    except ImportError:
        return host.encode("idna").decode("ascii")
    # idna.IDNAError subclasses UnicodeError.
    return _idna.encode(host, uts46=True).decode("ascii")


def _port_text(port: "int | str") -> str:
    """The digits of a port that is 0-65535 (an int, or a `str` of digits);
    anything else is a `ValueError`, so a field that did not come from a
    parse cannot add a path or a second authority to the result."""
    if isinstance(port, str):
        if not port.isascii() or not port.isdigit():
            raise ValueError("invalid port: it must be digits")
        return str(_parse_port(port))
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError("invalid port: it must be an integer")
    if not 0 <= port <= 65535:
        raise ValueError("port out of range 0-65535")
    return str(port)


def _compose_uri(
    scheme: "str | None",
    userinfo: "str | None",
    host: "str | _IPAddress | None",
    port: "int | str | None",
    path: str,
    query: "str | None",
    fragment: "str | None",
) -> str:
    parts = []
    if scheme is not None:
        if not _SCHEME_RE.fullmatch(scheme):
            raise ValueError("invalid scheme")
        parts.append(scheme)
        parts.append(":")
    has_authority = userinfo is not None or host is not None or port is not None
    if has_authority:
        parts.append("//")
        if userinfo is not None:
            parts.append(_encode_userinfo(userinfo))
            parts.append("@")
        if host is not None:
            parts.append(_compose_host(host))
        if port is not None:
            parts.append(":")
            parts.append(_port_text(port))
    path_enc = _encode(path, _SAFE_PATH) if path else ""
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
        parts.append(_encode_raw_query(query))
    if fragment is not None:
        parts.append("#")
        parts.append(_encode(fragment, _SAFE_FRAGMENT))
    return "".join(parts)


#: Schemes whose userinfo is an access token, not a user name: `str()` and
#: `repr()` show none of it (the classes drop it from their own text too).
_TOKEN_SCHEMES = frozenset({"github", "gitlab", "git", "git+github", "git+gitlab"})


def _redact_userinfo(userinfo: str, scheme: "str | None") -> "_Userinfo | None":
    """`userinfo` without its password; nothing at all for a token scheme,
    where the user name is the secret. An empty user name leaves no
    userinfo."""
    if scheme in _TOKEN_SCHEMES:
        return None
    user = _split_userinfo_text(userinfo)[0]
    return _Userinfo(user) if user else None


def _source_parts(source: "Source", sanitize: bool) -> tuple:
    """`(scheme, userinfo, host, port)` as `_compose_uri` takes them for
    `source`: falsy fields are left out and `sanitize` drops the password.
    A `Source` and a `Uri` both render through this, so they cannot
    disagree."""
    scheme = source.scheme.lower() if source.scheme else None
    userinfo = source.userinfo or None
    if sanitize and userinfo:
        userinfo = _redact_userinfo(userinfo, scheme)
    host = source.host if source.host else None
    port = source.port if source.port not in (None, "") else None
    return scheme, userinfo, host, port


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

    def _redacted_userinfo(self) -> "_Userinfo | None":
        if not self.userinfo:
            return self.userinfo
        scheme = self.scheme.lower() if self.scheme else None
        return _redact_userinfo(self.userinfo, scheme)

    def as_str(self, /, sanitize=True) -> str:
        """Compose this `Source` back into an authority string
        (`scheme://userinfo@host:port`; `file:` for an empty host). It is the
        composer `Uri.as_uri()` uses, so a non-ASCII host renders as IDNA
        here too, and a scheme or port that is not valid raises
        `ValueError`. `sanitize=True` (the default, matching `__str__`)
        drops the password from `userinfo`; pass `sanitize=False` for the
        full, credentialed round trip -- the same escape hatch
        `Uri.as_uri(sanitize=False)` provides one layer up.
        """
        scheme, userinfo, host, port = _source_parts(self, sanitize)
        return _compose_uri(scheme, userinfo, host, port, "", None, None)

    def __str__(self) -> str:
        """Deliberately sanitized (password dropped from `userinfo`), same
        rationale as `Uri.__str__`: this is what logging/printing reach
        for, and a `Source` on a failing call stack must not leak a
        credential. Does NOT round-trip a credentialed source -- use
        `as_str(sanitize=False)` for the unredacted form.
        """
        return self.as_str(sanitize=True)

    def __repr__(self) -> str:
        # NamedTuple's auto-generated __repr__ would include self.userinfo
        # verbatim (password and all) -- a traceback frame renders repr(),
        # so an unredacted Source anywhere on a failing call stack leaks
        # the credential into logs. Redact the same way __str__ does.
        return (
            f"{type(self).__name__}(scheme={self.scheme!r}, "
            f"userinfo={self._redacted_userinfo()!r}, host={self.host!r}, "
            f"port={self.port!r})"
        )

    @classmethod
    def from_str(cls, source: str, strict=True):
        """The `Source` a URI string names. With `strict` (the default) a
        path, query or fragment is a `ValueError` that names the component
        and not the input, which may carry a password. A port outside
        0-65535, or a ':' followed by anything but digits in a name that is
        not a bracketed address, is a `ValueError` as well."""
        uri = _uritools.urisplit(source)
        if strict:
            extra = [
                name for name in ("path", "query", "fragment") if getattr(uri, name)
            ]
            if extra:
                raise ValueError(
                    "expected scheme and authority only, found a "
                    + " and a ".join(extra)
                )
        scheme = uri.scheme.lower() if uri.scheme is not None else None
        return _parse_source(scheme, uri.authority)

    def keys(self):
        return self._asdict().keys()

    def __getitem__(self, key: int | slice | str):
        if isinstance(key, str):
            return getattr(self, key)
        return tuple.__getitem__(self, key)

    def parsed_userinfo(self) -> "tuple[str, str]":
        """`(user, password)`, `""` for a part that is absent. The split is
        the first colon of the userinfo as it was written (an escaped colon
        in the user name stays in the name)."""
        if not self.userinfo:
            return "", ""
        user, password = _split_userinfo_text(self.userinfo)
        return user, password or ""

    def get_scheme_cls(self, schemesmap: _ty.Mapping[str, type["UriPath"]] = None):
        """The `UriPath` subclass registered for this scheme, or `UriPath`
        itself. An explicit `schemesmap` is the complete set of classes to
        choose from: a scheme missing from it gives `UriPath`, and neither
        plugins nor the global registry are consulted."""
        from . import UriPath

        if self.scheme:
            if schemesmap is not None:
                return schemesmap.get(self.scheme, None) or UriPath
            schemesmap = UriPath._schemesmap()
            _cls = schemesmap.get(self.scheme, None)
            if _cls is None:
                # The map is rebuilt whenever a UriPath subclass is defined
                # (UriPath.__init_subclass__), so a miss here is a scheme no
                # imported class registers; `_load_entry_point` caches that
                # negative answer instead of rescanning every installed
                # distribution on each construction.
                if UriPath._load_entry_point(
                    self.scheme
                ) or UriPath._load_builtin_scheme(self.scheme):
                    schemesmap = UriPath._schemesmap(reload=True)
                    _cls = schemesmap.get(self.scheme, None)
            return _cls if _cls else UriPath
        return UriPath

    def is_local(self) -> bool:
        """Whether `host` resolves to this machine.

        The answer depends on the host alone (userinfo, port and scheme play
        no part) and is remembered for `_LOCAL_TTL` seconds, at most
        `_LOCAL_CACHE_SIZE` hosts: it comes from a DNS/hosts-file lookup and
        the machine's interfaces, so it is neither free nor forever.

        The hostname->address step uses `netimps.resolve()` (default
        backend chain: dnspython, then the OS resolver via
        `getaddrinfo()` -- hosts file, NSS, DNS, OS cache -- then
        `nslookup` as a last resort), trying both `"a"`/`"aaaa"` record
        types and treating `host` as local if ANY resolved address is.
        The "is this address MINE" comparison uses
        `netimps.is_local_address()`, which enumerates the real network
        interfaces (`netimps.get_interfaces()`), so VMs, containers, VPN
        interfaces and additional NICs count.

        `host` as a bare IP-literal `str` (e.g. a directly-constructed
        `Source(..., host="::1", ...)`, bypassing `_decode_host()`'s usual
        bracket-literal parsing) is handled by `netimps.try_parse()`
        without going through resolution at all.
        """
        host = self.host
        if not host or host == "localhost":
            return True
        return _host_is_local(host)


#: How long `Source.is_local()` remembers an answer, in seconds, and for how
#: many hosts.
_LOCAL_TTL = 60.0
_LOCAL_CACHE_SIZE = 256
_local_cache: "dict[object, tuple[float, bool]]" = {}
_local_lock = _threading.Lock()


def _host_is_local(host: "str | _IPAddress") -> bool:
    now = _time.monotonic()
    with _local_lock:
        entry = _local_cache.get(host)
    if entry is not None and entry[0] > now:
        return entry[1]
    answer = _resolve_is_local(host)
    with _local_lock:
        if len(_local_cache) >= _LOCAL_CACHE_SIZE:
            for key in [k for k, (until, _) in _local_cache.items() if until <= now]:
                del _local_cache[key]
        while len(_local_cache) >= _LOCAL_CACHE_SIZE:
            del _local_cache[next(iter(_local_cache))]
        _local_cache[host] = (now + _LOCAL_TTL, answer)
    return answer


def _resolve_is_local(host: "str | _IPAddress") -> bool:
    # Imported here, not at module top: only this needs netimps, and every
    # `import pathlib_next` would otherwise pay for its import.
    try:
        import netimps as _netimps
    except ImportError as error:
        raise ImportError(
            'netimps is not installed: pip install "pathlib-next[uri]"',
            name="netimps",
        ) from error

    if not isinstance(host, str):
        return _netimps.is_local_address(host)
    literal = _netimps.try_parse(host)
    if literal is not None:
        return _netimps.is_local_address(literal)
    addresses = _netimps.resolve(host, "a") + _netimps.resolve(host, "aaaa")
    return any(_netimps.is_local_address(address) for address in addresses)


_NOSOURCE = Source(None, None, None, None)
