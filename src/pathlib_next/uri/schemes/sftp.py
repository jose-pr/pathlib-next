from __future__ import annotations

import threading as _thread
import typing as _ty

import paramiko as _paramiko

from ... import utils as _utils
from .. import Source, Uri, UriPath


class BaseSftpBackend(object):
    """Protocol for obtaining a paramiko `SFTPClient` for a `Source`.
    Subclass this to plug in custom connection handling (e.g. tests mock
    it directly, no real server); `SftpBackend` is the real implementation."""

    __slots__ = ()

    @_utils.notimplemented
    def client(self, source: Source) -> _paramiko.SFTPClient: ...


class SftpBackend(BaseSftpBackend):
    """Connects via `paramiko.SSHClient` using `connect_opts` merged with
    the `Source`'s host/port/userinfo."""

    __slots__ = ("connect_opts", "hostkeypolicy")
    connect_opts: dict[str, str]
    hostkeypolicy: _paramiko.MissingHostKeyPolicy

    def __init__(self, connect_opts, hostkeypolicy) -> None:
        self.connect_opts = connect_opts
        self.hostkeypolicy = hostkeypolicy

    def opts(self, source: Source):
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

    def transport(self, source: Source) -> _paramiko.Transport:
        client = _paramiko.SSHClient()
        client.set_missing_host_key_policy(self.hostkeypolicy)
        client.connect(**self.opts(source))
        transport = client.get_transport()
        if not transport:
            raise Exception()
        return transport

    def client(self, source: Source):
        return self.transport(source).open_sftp_client()


def _create_sftpclient(backend: BaseSftpBackend, source: Source, thread_id: int):
    return backend.client(source)


_CACHED_CLIENTS = _utils.LRU(_create_sftpclient, maxsize=128)


class SftpPath(UriPath):
    """`sftp:` scheme: full read/write access via paramiko, with a
    thread-keyed LRU connection cache (`_CACHED_CLIENTS`). Requires the
    `sftp` extra."""

    __SCHEMES = ("sftp",)
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseSftpBackend

    def _initbackend(self):
        return SftpBackend({}, _paramiko.MissingHostKeyPolicy)

    @property
    def _sftpclient(self):
        thead_id = _thread.get_ident()
        client = _CACHED_CLIENTS(self.backend, self.source, thead_id)
        if client is None or not client.sock.active:
            client = _CACHED_CLIENTS.invalidate(self.backend, self.source, thead_id)
        return client

    def _listdir(self):
        for path in self._sftpclient.listdir(self.path):
            yield path

    def stat(self, *, follow_symlinks=True):
        if follow_symlinks:
            return self._sftpclient.stat(self.path)
        else:
            return self._sftpclient.lstat(self.path)

    def _open(self, mode="r", buffering=-1):
        return self._sftpclient.open(self.path, mode, buffering)

    def _mkdir(self, mode):
        return self._sftpclient.mkdir(self.path, mode)

    def chmod(self, mode, *, follow_symlinks=True):
        # paramiko's SFTPClient has no lchmod-equivalent.
        if not follow_symlinks:
            raise NotImplementedError("chmod(follow_symlinks=False)")
        return self._sftpclient.chmod(self.path, mode)

    def unlink(self, missing_ok=False):
        if missing_ok and not self.exists():
            return
        return self._sftpclient.remove(self.path)

    def rmdir(self):
        return self._sftpclient.rmdir(self.path)

    def rename(self, target: "SftpPath | Uri | str"):
        # base Path.rename is the notimplemented stub -- this was never
        # called under its old name `_rename`, so every move() fell back to
        # copy+unlink. `target.path`, not as_posix(): Uri.as_posix() prefixes
        # "host:" for the sftp wire protocol, which only wants the raw path.
        # A plain str target is resolved relative to self's *parent*
        # (sibling rename -- "rename this file to a new name in the same
        # directory"), not to self itself (which would join it as a child).
        if not isinstance(target, Uri):
            target = Uri(self.parent, target)
        return self._sftpclient.rename(self.path, target.path)
