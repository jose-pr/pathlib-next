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
        return cls(uri.getscheme(), uri.getuserinfo(), uri.gethost(), uri.getport())

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
