from __future__ import annotations

import pathlib as _pathlib
import threading as _thread

import paramiko as _paramiko

from .... import utils as _utils
from ... import Source
from . import BaseSftpBackend

# The sentinel + path normalization are paramiko-free and now live in
# ``_sshconfig`` so the asyncssh backend and the scheme ``__init__`` can use them
# without importing paramiko. Re-exported here for backward compatibility (older
# code did ``from ._paramiko import _DEFAULT_SSH_CONFIG``).
from ._sshconfig import _DEFAULT_SSH_CONFIG, _normalize_config_paths


@_utils.LRU
def _load_ssh_config(config_paths: "tuple[str, ...]") -> "_paramiko.SSHConfig | None":
    config = _paramiko.SSHConfig()
    loaded = False
    for path in config_paths:
        ssh_path = _pathlib.Path(path).expanduser()
        if not ssh_path.is_file():
            continue
        with ssh_path.open(encoding="utf-8") as handle:
            config.parse(handle)
        loaded = True
    return config if loaded else None


def _lookup_ssh_config(
    host: str,
    ssh_config: "object",
) -> "dict[str, object]":
    config_paths = _normalize_config_paths(ssh_config)
    if not config_paths:
        return {}
    config = _load_ssh_config(config_paths)
    if config is None:
        return {}
    return config.lookup(host)


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

    __slots__ = ("connect_opts", "hostkeypolicy", "ssh_config")
    connect_opts: dict[str, str]
    hostkeypolicy: _paramiko.MissingHostKeyPolicy

    def __init__(
        self,
        connect_opts,
        hostkeypolicy,
        ssh_config=_DEFAULT_SSH_CONFIG,
    ) -> None:
        self.connect_opts = connect_opts
        self.hostkeypolicy = hostkeypolicy
        self.ssh_config = ssh_config

    def opts(self, source: Source):
        config = _lookup_ssh_config(str(source.host), self.ssh_config)
        connect_ops = {
            **self.connect_opts,
            "hostname": config.get("hostname", str(source.host)),
            "port": source.port or int(config.get("port", 22)),
        }
        user, password = source.parsed_userinfo()
        if user:
            connect_ops["username"] = user
        elif "username" not in connect_ops and "user" in config:
            connect_ops["username"] = str(config["user"])
        if password:
            connect_ops["password"] = password
        if "key_filename" not in connect_ops and "identityfile" in config:
            connect_ops["key_filename"] = list(config["identityfile"])
        if "sock" not in connect_ops and "proxycommand" in config:
            connect_ops["sock"] = _paramiko.ProxyCommand(str(config["proxycommand"]))
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
    def default(cls, ssh_config=_DEFAULT_SSH_CONFIG) -> "SftpBackend":
        return cls({}, _paramiko.MissingHostKeyPolicy, ssh_config=ssh_config)
