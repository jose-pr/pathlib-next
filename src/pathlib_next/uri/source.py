import typing as _ty
import uritools as _uritools
import socket as _socket
import ipaddress as _ip

from .. import utils as _utils

_IPAddress = _ip.IPv4Address | _ip.IPv6Address

if _ty.TYPE_CHECKING:
    from . import Uri


class Source(_ty.NamedTuple):
    scheme: str
    userinfo: str
    host: str | _IPAddress
    port: int

    def __bool__(self):
        if not self.scheme:
            return False
        return True

    def __str__(self) -> str:
        return _uritools.uricompose(
            scheme=self.scheme, userinfo=self.userinfo, host=self.host, port=self.port
        )

    def parsed_userinfo(self):
        parts = []
        if self.userinfo:
            parts = self.userinfo.split(":", maxsplit=1)
        parts = parts + ["", ""]
        return parts[0], parts[1]

    def get_scheme_cls(self, schemesmap: _ty.Mapping[str, type["Uri"]] = None):
        from . import Uri
        if self.scheme:
            if schemesmap is None:
                schemesmap = Uri._schemesmap()
            _cls = schemesmap.get(self.scheme, None)
            return _cls if _cls else Uri
        return Uri

    def is_local(self):
        host = self.host
        if not host or host == "localhost":
            return True
        if isinstance(host, str):
            host = _ip.ip_address(_socket.gethostbyname(host))
        return host.is_loopback or host in _utils.get_machine_ips()


_NOSOURCE = Source(None, None, None, None)