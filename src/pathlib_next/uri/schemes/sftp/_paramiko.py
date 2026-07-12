from __future__ import annotations

import threading as _thread

import paramiko as _paramiko

from .... import utils as _utils
from ... import Source
from . import BaseSftpBackend


def _create_sftpclient(backend: "SftpBackend", source: Source, thread_id: int):
    return backend.transport(source).open_sftp_client()


# Thread-keyed: paramiko's client is bound to the thread that owns its
# socket-reading loop, so the cache key includes thread_id (unlike
# asyncssh's single-shared-loop backend, which doesn't need that
# dimension). Module-level (not per-backend-instance) so every SftpBackend
# instance shares the same cache, keyed by (backend, source, thread_id) --
# matches this module's pre-package-split behavior exactly.
_CACHED_CLIENTS = _utils.LRU(_create_sftpclient, maxsize=128)


class SftpBackend(BaseSftpBackend):
    """Connects via `paramiko.SSHClient` using `connect_opts` merged with
    the `Source`'s host/port/userinfo. `client()` caches per
    `(self, source, calling-thread)` -- see `_CACHED_CLIENTS` above."""

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
        thread_id = _thread.get_ident()
        client = _CACHED_CLIENTS(self, source, thread_id)
        if client is None or not client.sock.active:
            client = _CACHED_CLIENTS.invalidate(self, source, thread_id)
        return client

    @classmethod
    def default(cls) -> "SftpBackend":
        return cls({}, _paramiko.MissingHostKeyPolicy)
