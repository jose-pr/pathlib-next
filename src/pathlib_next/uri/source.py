import typing as _ty
import uritools as _uritools
import socket as _socket
import ipaddress as _ip

from .. import utils as _utils

_IPAddress = _ip.IPv4Address | _ip.IPv6Address

if _ty.TYPE_CHECKING:
    from . import UriPath


class Source(_ty.NamedTuple):
    scheme: str | None
    userinfo: str | None
    host: str | _IPAddress | None
    port: int | None

    def __bool__(self):
        for val in self:
            if val == '' or val is None:
                continue
            return True
        return False

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
            return _cls if _cls else UriPath
        return UriPath

    def is_local(self):
        host = self.host
        if not host or host == "localhost":
            return True
        if isinstance(host, str):
            host = _ip.ip_address(_socket.gethostbyname(host))
        return host.is_loopback or host in _utils.get_machine_ips()


_NOSOURCE = Source(None, None, None, None)
