from pathlib import Path as _Path
import typing as _ty
from ..uri import Uri
from .. import utils as _utils

import ipaddress as _ipaddress

import socket as _socket

class FileUri(Uri):
    _SCHEMES_ = ["file"]
    __slots__ = ('_filepath',)

    @property
    def filepath(self):
        if self._filepath is None:
            if self.source.host and self.source.host != 'localhost':
                host = self.source.host
                if isinstance(host, str):
                    host = _ipaddress.ip_address(_socket.gethostbyname(host))
                if not host.is_loopback:
                    if host not in _utils.get_machine_ips():
                        raise FileNotFoundError(self)
            self._filepath = _Path(self.path)
        return self._filepath
    
    def iterdir(self):
        for path in self.filepath.iterdir():
            yield type(self)(path)

    def stat(self, *, follow_symlinks=True):
        return self.filepath.stat(follow_symlinks=follow_symlinks)

    def open(
        self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
    ):
        return self.filepath.open(mode, buffering, encoding, errors, newline)

    def mkdir(self, mode=511, parents=False, exist_ok=False):
        return self.filepath.mkdir(mode, parents, exist_ok)

    def chmod(self, mode, *, follow_symlinks=True):
        return self.filepath.chmod(mode, follow_symlinks=follow_symlinks)

    def unlink(self, missing_ok=False):
        return self.filepath.unlink(missing_ok)

    def rmdir(self):
        return self.filepath.rmdir()
    
    def _rename(self, target):
        return self.filepath.rename(target)