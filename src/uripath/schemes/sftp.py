import typing as _ty
from ..uri import Uri, UriSource
from .. import utils as _utils

import paramiko as _paramiko


class BaseSftpBackend(object):
    __slots__ = ()
    @_utils.notimplemented
    def client(self, source: UriSource) -> _paramiko.SFTPClient: ...


class SftpBackend(BaseSftpBackend):
    __slots__ = ("_client", "connect_opts", "hostkeypolicy")
    _client: _paramiko.SFTPClient
    connect_opts: dict[str, str]
    hostkeypolicy: _paramiko.MissingHostKeyPolicy

    def __init__(self, connect_opts, hostkeypolicy) -> None:
        self._client = None
        self.connect_opts = connect_opts
        self.hostkeypolicy = hostkeypolicy

    def opts(self, source: UriSource):
        connect_ops = {
            **self.connect_opts,
            "hostname": str(source.host),
            "port": source.port or 22,
        }
        user, password = source.parsed_userinfo()
        if user:
            connect_ops["username"] = user
        if password:
            connect_ops["password"] = password
        return connect_ops

    def transport(self, source: UriSource) -> _paramiko.Transport:
        client = _paramiko.SSHClient()
        client.set_missing_host_key_policy(self.hostkeypolicy)
        client.connect(**self.opts(source))
        transport = client.get_transport()
        if not transport:
            raise Exception()
        return transport

    def client(self, source: UriSource):
        if self._client is None or not self._client.sock.active:
            self._client = self.transport(source).open_sftp_client()
        return self._client


class SftpPath(Uri):

    _SCHEMES_ = ["sftp"]
    __slots__ = ("_remote_path",)

    if _ty.TYPE_CHECKING:
        backend: BaseSftpBackend

    def _initbackend(self):
        return SftpBackend({}, _paramiko.MissingHostKeyPolicy)

    @property
    def remote_path(self):
        if self._remote_path is None:
            self._remote_path = self.path.as_posix()
        return self._remote_path

    @property
    def _sftpclient(self):
        return self.backend.client(self.source)

    def iterdir(self) -> _ty.Iterable["SftpPath"]:
        for path in self._sftpclient.listdir(self.remote_path):
            yield self / path

    def stat(self):
        return self._sftpclient.stat(self.remote_path)

    def _open(self, mode="r", buffering=-1):
        return self._sftpclient.open(self.remote_path, mode, buffering)

    def _mkdir(self, mode):
        return self._sftpclient.mkdir(self.remote_path, mode)

    def chmod(self, mode):
        return self._sftpclient.chmod(self.remote_path, mode)

    def unlink(self, missing_ok=False):
        if missing_ok and not self.exists():
            return
        return self._sftpclient.remove(self.remote_path)

    def rmdir(self):
        return self._sftpclient.rmdir(self.remote_path)

    def _rename(self, target):
        return self._sftpclient.rename(self.remote_path, target.as_posix())
