from __future__ import annotations

import pathlib as _pathlib
import threading as _thread
import weakref as _weakref

import netimps as _netimps
import paramiko as _paramiko
import paramiko.sftp as _paramiko_sftp

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

# Algorithm names the OpenSSH check-file@openssh.com extension protocol
# itself supports (https://github.com/openssh/openssh-portable/blob/
# master/PROTOCOL) -- the full candidate set a real probe tries, in
# preference order (md5 first: matches PathSyncer's current default, so
# the common case resolves in exactly one round trip).
_CHECK_FILE_ALGORITHMS = ("md5", "sha1", "sha256", "sha384", "sha512")

# Per-connection cache of which algorithms a real probe has confirmed the
# server supports (see SftpBackend.supported_checksums) -- keyed by the SFTP
# client object itself (WeakKeyDictionary, not id(): a plain dict keyed by
# id() risks a stale hit if a client is GC'd and a new, unrelated object
# happens to get the same id() -- a real risk here since _CACHED_CLIENTS
# above can evict/replace clients over a long-running process). A reconnect
# (new client instance, e.g. after the old socket went inactive -- see
# SftpBackend.client()) naturally starts with a clean slate. Paramiko
# exposes no public way to read the server's advertised extension list from
# version negotiation (_send_version() reads and discards that part of the
# CMD_VERSION reply), so an actual attempt against a real file is the only
# reliable source of truth.
_CHECKSUM_SUPPORT_CACHE: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()
_CHECKSUM_SUPPORT_LOCK = _thread.Lock()


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
            "port": source.port or int(config.get("port", _netimps.get_default_port("sftp"))),
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

    def checksum(self, path: "SftpPath", algorithm: str) -> str:
        """Server-side digest via the OpenSSH `check-file@openssh.com` SFTP
        protocol extension (https://github.com/openssh/openssh-portable/
        blob/master/PROTOCOL, "check-file@openssh.com"). Not part of
        paramiko's public API -- built on the same low-level
        `_request(CMD_EXTENDED, ...)` primitive paramiko itself uses
        internally for `posix-rename@openssh.com` (see
        `SFTPClient.posix_rename`). Requires an actual OpenSSH (or
        compatible) server; anything else -- including this project's own
        asyncssh-backed test server (`tests/conftest.py::sftp_server`,
        which has no `check-file@openssh.com` support at all) -- fails and
        is translated to `NotImplementedError` by `SftpPath.checksum()`.
        """
        client = self.client(path.source)
        # check-file@openssh.com hashes an *open handle*, not a bare path --
        # open read-only, always close even on failure so a checksum
        # attempt (whether it succeeds, or the server simply doesn't
        # support the extension) never leaks a file handle.
        handle_file = client.open(path.path, "r")
        try:
            handle = handle_file.handle
            msg_type, msg = client._request(
                _paramiko_sftp.CMD_EXTENDED,
                "check-file@openssh.com",
                handle,
                algorithm,
                # int64(...): a plain `int` would be packed as a 32-bit
                # int by Message.add() (see paramiko's _async_request
                # arg-type dispatch) -- the extension's wire format
                # requires uint64 for offset/length.
                _paramiko_sftp.int64(0),  # offset
                _paramiko_sftp.int64(0),  # length: 0 means "whole file"
                0,  # quick_check: 0 requests a real hash, not a fast probe
            )
        finally:
            handle_file.close()
        if msg_type != _paramiko_sftp.CMD_EXTENDED_REPLY:
            raise NotImplementedError(
                "check-file@openssh.com: unexpected reply type "
                f"{msg_type!r} (server likely doesn't support this "
                "extension)"
            )
        reply_algorithm = msg.get_text()
        if reply_algorithm != algorithm:
            # A server MUST echo back one of the algorithms we offered --
            # if it names something else, don't trust the digest.
            raise NotImplementedError(
                f"check-file@openssh.com returned {reply_algorithm!r}, "
                f"requested {algorithm!r}"
            )
        return msg.get_binary().hex()

    def supported_checksums(self, path: "SftpPath") -> "frozenset[str]":
        """Real per-connection probe: try `checksum()` against `path` for
        every algorithm `check-file@openssh.com` itself supports, and cache
        which ones this specific connected client actually accepted (see
        `_CHECKSUM_SUPPORT_CACHE` above -- paramiko has no cheaper way to
        learn this). `path` must already exist and be readable, or the
        probe's own `open()` fails for an unrelated reason (a missing
        file), which is reported the same as "unsupported" here -- this
        method is advisory, never raises.

        Only the FIRST algorithm is actually attempted once a connection's
        support is confirmed for any algorithm: a real OpenSSH server
        either implements `check-file@openssh.com` (and lists it in its
        extension advertisement, which implies every algorithm from the
        RFC), or it doesn't -- there is no realistic "supports md5 but not
        sha256" split in practice, and re-probing every algorithm every
        time would multiply round trips for no real benefit. If a caller
        actually needs a different specific algorithm to be reconfirmed
        against a server with genuinely partial support, `checksum()`
        itself remains authoritative and still raises `NotImplementedError`
        per call regardless of what this advertises.
        """
        client = self.client(path.source)
        with _CHECKSUM_SUPPORT_LOCK:
            cached = _CHECKSUM_SUPPORT_CACHE.get(client)
        if cached is not None:
            return cached

        supported = frozenset()
        try:
            self.checksum(path, _CHECK_FILE_ALGORITHMS[0])
        except NotImplementedError:
            supported = frozenset()
        except Exception:
            # Any other failure (missing file, transport hiccup, ...) isn't
            # evidence one way or the other about extension support --
            # don't cache a negative result from an inconclusive probe.
            return frozenset()
        else:
            supported = frozenset(_CHECK_FILE_ALGORITHMS)

        with _CHECKSUM_SUPPORT_LOCK:
            _CHECKSUM_SUPPORT_CACHE[client] = supported
        return supported

    @classmethod
    def default(cls, ssh_config=_DEFAULT_SSH_CONFIG) -> "SftpBackend":
        return cls({}, _paramiko.MissingHostKeyPolicy, ssh_config=ssh_config)
