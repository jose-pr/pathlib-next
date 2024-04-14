import typing as _ty
from .uri import UriPath

import paramiko as _paramiko


class SftpPath(UriPath):

    _SCHEMES_ = ["sftp"]
    _client: _paramiko.SSHClient
    _connect_opts: dict[str, str]

    __slots__ = ("_client", "_connect_opts", "_remote_path")

    @property
    def client(self):
        if self._client is None:
            self._client = _paramiko.SSHClient()
            self._client.set_missing_host_key_policy(_paramiko.MissingHostKeyPolicy)
        return self._client

    @property
    def connect_opts(self):
        if self._connect_opts is None:
            self._connect_opts = {}
        return self._connect_opts

    @property
    def remote_path(self):
        if self._remote_path is None:
            self._remote_path = self.as_posix()
        return self._remote_path

    def _sftpclient(self):
        transport = self.client.get_transport()
        if not transport or not transport.is_authenticated():
            connect_ops = {}
            user, password = self.source.parsed_userinfo()
            if user:
                connect_ops["username"] = user
            if password:
                connect_ops["password"] = password
            connect_ops.update(self.connect_opts)
            self.client.connect(
                str(self.source.host), port=self.source.port or 22, **connect_ops
            )
        return self.client.open_sftp()

    def iterdir(self) -> _ty.Iterable["SftpPath"]:
        for path in self._sftpclient().listdir(self.remote_path):
            yield self / path

    def stat(self):
        return self._sftpclient().stat(self.remote_path)

    def _open(self, mode="r", buffering=-1):
        return self._sftpclient().open(self.remote_path, mode, buffering)

    def _mkdir(self, mode):
        return self._sftpclient().mkdir(self.remote_path, mode)

    def chmod(self, mode):
        return self._sftpclient().chmod(self.remote_path, mode)

    def unlink(self, missing_ok=False):
        if missing_ok and not self.exists():
            return
        return self._sftpclient().remove(self.remote_path)

    def rmdir(self):
        return self._sftpclient().rmdir(self.remote_path)
    
    def _rename(self, target):
        return self._sftpclient().rename(self.remote_path, target.as_posix())