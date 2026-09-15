from __future__ import annotations

import hashlib as _hashlib
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
from ._sshconfig import (
    _DEFAULT_SSH_CONFIG,
    _expand_includes,
    _normalize_config_paths,
)

#: Sentinel for `SftpBackend(known_hosts=...)`: the user's `~/.ssh/known_hosts`
#: plus every `UserKnownHostsFile` the ssh_config names for the host.
_DEFAULT_KNOWN_HOSTS = object()


@_utils.LRU
def _load_ssh_config(config_paths: "tuple[str, ...]") -> "_paramiko.SSHConfig | None":
    config = _paramiko.SSHConfig()
    loaded = False
    for path in config_paths:
        ssh_path = _pathlib.Path(path).expanduser()
        if not ssh_path.is_file():
            continue
        # paramiko's parser has no Include support: expand it first.
        config.parse(_expand_includes(ssh_path))
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
    transport = backend.transport(source)
    try:
        return transport.open_sftp_client()
    except BaseException:
        transport.close()
        raise


def _close_sftpclient(key: tuple, client) -> None:
    # An SFTPClient going out of scope closes nothing: its Transport is a
    # running thread that the threading module keeps alive, so an evicted or
    # dead client must have its transport closed explicitly.
    try:
        client.close()
    finally:
        transport = client.sock.get_transport()
        if transport is not None:
            transport.close()


def _client_is_alive(client) -> bool:
    # `Channel.active` is set once on open and never cleared on close, so it
    # cannot detect a dropped connection on its own; `closed` and the
    # transport's own liveness can.
    if client is None:
        return False
    channel = getattr(client, "sock", None)
    if channel is None:
        return False
    if not getattr(channel, "active", True) or getattr(channel, "closed", False):
        return False
    get_transport = getattr(channel, "get_transport", None)
    if get_transport is None:
        return True
    transport = get_transport()
    return transport is not None and bool(transport.is_active())


# Thread-keyed: paramiko's client is bound to the thread that owns its
# socket-reading loop, so the cache key includes thread_id (unlike
# asyncssh's single-shared-loop backend, which doesn't need that
# dimension). Module-level (not per-backend-instance) so every SftpBackend
# instance shares the same cache, keyed by (backend, source, thread_id) --
# matches this module's pre-package-split behavior exactly. Evicted and
# invalidated clients have their transport closed (`_close_sftpclient`).
_CACHED_CLIENTS = _utils.LRU(
    _create_sftpclient, maxsize=128, on_evict=_close_sftpclient
)

#: The SFTP extension request `SftpBackend.checksum()` sends: the filexfer
#: draft's `check-file-handle` (draft-ietf-secsh-filexfer-extensions-00,
#: section 3; implemented by e.g. ProFTPD's mod_sftp). OpenSSH implements no
#: check-file extension at all -- its sftp-server answers
#: SSH_FX_OP_UNSUPPORTED, which is cached per connection (see
#: `_CHECKSUM_SUPPORT_CACHE`) so it costs one request, not one per file.
_CHECK_FILE_EXTENSION = "check-file-handle"

# Hash algorithm names from the draft, in preference order (md5 first:
# PathSyncer's default, so the common case resolves in one round trip).
_CHECK_FILE_ALGORITHMS = ("md5", "sha1", "sha256", "sha384", "sha512")

# Per-connection cache of which algorithms the server supports (see
# SftpBackend.supported_checksums) -- keyed by the SFTP client object itself
# (WeakKeyDictionary, not id(): a plain dict keyed by id() risks a stale hit
# if a client is GC'd and a new, unrelated object happens to get the same
# id() -- a real risk here since _CACHED_CLIENTS above can evict/replace
# clients over a long-running process). A reconnect (new client instance,
# e.g. after the old socket went inactive -- see SftpBackend.client())
# naturally starts with a clean slate. Paramiko exposes no public way to
# read the server's advertised extension list from version negotiation
# (_send_version() reads and discards that part of the CMD_VERSION reply),
# so an actual attempt against a real file is the only source of truth. An
# empty set is a definitive "unsupported": `checksum()` then raises
# NotImplementedError without sending anything.
_CHECKSUM_SUPPORT_CACHE: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()
_CHECKSUM_SUPPORT_LOCK = _thread.Lock()


class SftpBackend(BaseSftpBackend):
    """Connects via `paramiko.SSHClient` using `connect_opts` merged with
    the `Source`'s host/port/userinfo. `client()` caches per
    `(self, source, calling-thread)` -- see `_CACHED_CLIENTS` above -- and
    replaces (and closes) a cached client whose connection has dropped;
    `close()` closes every connection the backend opened.

    Host keys are verified by default. `known_hosts` (default: the user's
    `~/.ssh/known_hosts` plus any ssh_config `UserKnownHostsFile` for the
    host; `None` loads no file; a path or iterable of paths loads exactly
    those) is loaded read-only, and `hostkeypolicy` (default
    `paramiko.RejectPolicy()`) decides what happens to a key found in none
    of them. A key that differs from a known one always raises
    `paramiko.BadHostKeyException`. **Opt-out**, in code only:
    `SftpBackend(connect_opts, paramiko.AutoAddPolicy(), known_hosts=None)`
    accepts any server key -- a network man-in-the-middle then receives the
    URI password.

    `timeout` (default 30 s) is the default for paramiko's `timeout`,
    `banner_timeout`, `auth_timeout` and `channel_timeout` connect options;
    a value in `connect_opts` wins, and `timeout=None` leaves them unset.
    Individual SFTP requests on an established connection are not bounded.

    ssh_config support is paramiko's own (`HostName`, `Port`, `User`,
    `IdentityFile`, `ProxyCommand`) plus `Include`. `ProxyJump` is not
    supported: a host whose config sets it raises `NotImplementedError`
    unless `connect_opts` supplies a `sock` -- use the asyncssh backend, or
    an equivalent `ProxyCommand`."""

    __slots__ = (
        "connect_opts",
        "hostkeypolicy",
        "ssh_config",
        "known_hosts",
        "timeout",
    )
    connect_opts: dict[str, str]
    hostkeypolicy: _paramiko.MissingHostKeyPolicy

    #: Default connect/banner/auth/channel-open timeout, in seconds.
    DEFAULT_TIMEOUT = 30.0

    def _wire_open_mode(self, mode: str) -> str:
        # paramiko derives SFTP_FLAG_WRITE (and the file object's
        # writability) from "w"/"a"/"+" only, so a bare "x" created a file
        # that could not be written. "w" adds CREATE|TRUNC, which EXCL makes
        # moot: the file must not exist yet.
        if "x" in mode and "w" not in mode:
            return "w" + mode
        return mode

    def __init__(
        self,
        connect_opts=None,
        hostkeypolicy=None,
        ssh_config=_DEFAULT_SSH_CONFIG,
        *,
        known_hosts=_DEFAULT_KNOWN_HOSTS,
        timeout: "float | None" = DEFAULT_TIMEOUT,
    ) -> None:
        self.connect_opts = {} if connect_opts is None else connect_opts
        self.hostkeypolicy = (
            _paramiko.RejectPolicy() if hostkeypolicy is None else hostkeypolicy
        )
        self.ssh_config = ssh_config
        self.known_hosts = known_hosts
        self.timeout = timeout

    def opts(self, source: Source):
        config = _lookup_ssh_config(str(source.host), self.ssh_config)
        connect_ops = {
            **self.connect_opts,
            "hostname": config.get("hostname", str(source.host)),
            "port": source.port
            or int(config.get("port", _netimps.get_default_port("sftp"))),
        }
        if self.timeout is not None:
            for key in ("timeout", "banner_timeout", "auth_timeout", "channel_timeout"):
                connect_ops.setdefault(key, self.timeout)
        user, password = source.parsed_userinfo()
        if user:
            connect_ops["username"] = user
        elif "username" not in connect_ops and "user" in config:
            connect_ops["username"] = str(config["user"])
        if password:
            connect_ops["password"] = password
        if "key_filename" not in connect_ops and "identityfile" in config:
            connect_ops["key_filename"] = list(config["identityfile"])
        if "sock" not in connect_ops:
            if config.get("proxycommand"):
                connect_ops["sock"] = _paramiko.ProxyCommand(
                    str(config["proxycommand"])
                )
            elif str(config.get("proxyjump") or "none").lower() != "none":
                # Connecting directly would silently bypass the jump host.
                raise NotImplementedError(
                    f"ssh_config ProxyJump ({config['proxyjump']}) for host "
                    f"{source.host!r} is not supported by the paramiko SFTP "
                    "backend -- use the asyncssh backend ('sftp-async' extra), "
                    "an equivalent ProxyCommand, or connect_opts['sock']"
                )
        return connect_ops

    def _known_hosts_files(self, source: Source) -> "list[str]":
        known_hosts = self.known_hosts
        if known_hosts is None:
            return []
        if known_hosts is not _DEFAULT_KNOWN_HOSTS:
            if isinstance(known_hosts, (str, _pathlib.PurePath)):
                return [str(known_hosts)]
            return [str(path) for path in known_hosts]
        home = str(_pathlib.Path.home())
        files = [str(_pathlib.Path(home, ".ssh", "known_hosts"))]
        config = _lookup_ssh_config(str(source.host), self.ssh_config)
        for value in str(config.get("userknownhostsfile") or "").split():
            if value.lower() != "none":
                files.append(str(_pathlib.Path(value.replace("%d", home)).expanduser()))
        # Default locations are optional; explicitly named files are not.
        return [path for path in files if _pathlib.Path(path).is_file()]

    def transport(self, source: Source) -> _paramiko.Transport:
        opts = self.opts(source)
        client = _paramiko.SSHClient()
        try:
            for path in self._known_hosts_files(source):
                # load_system_host_keys(): read-only, never written back --
                # an AutoAddPolicy must not edit the user's files either.
                client.load_system_host_keys(path)
            client.set_missing_host_key_policy(self.hostkeypolicy)
            client.connect(**opts)
            transport = client.get_transport()
            if not transport:
                raise _paramiko.SSHException("connect() produced no transport")
        except BaseException:
            # A failed connect/auth still leaves a running Transport thread
            # and an open socket (or a ProxyCommand subprocess) behind.
            client.close()
            sock = opts.get("sock")
            if sock is not None and "sock" not in self.connect_opts:
                try:
                    sock.close()
                except Exception:
                    pass
            raise
        return transport

    def client(self, source: Source):
        thread_id = _thread.get_ident()
        client = _CACHED_CLIENTS(self, source, thread_id)
        if not _client_is_alive(client):
            client = _CACHED_CLIENTS.invalidate(self, source, thread_id)
        return client

    def close(self) -> None:
        """Close every cached connection this backend opened, on any thread."""
        for key in list(_CACHED_CLIENTS.cache):
            if key[0] is self:
                _CACHED_CLIENTS.discard(*key)

    def checksum(self, path: "SftpPath", algorithm: str) -> str:
        """Server-side digest via the filexfer draft's `check-file-handle`
        SFTP extension (see `_CHECK_FILE_EXTENSION`). Not part of paramiko's
        public API -- built on the same low-level `_request(CMD_EXTENDED,
        ...)` primitive paramiko itself uses for `posix-rename@openssh.com`.
        OpenSSH does not implement it, nor does this project's asyncssh
        test server (`tests/conftest.py::sftp_server`): the first refusal is
        cached for the connection, and every later call raises
        `NotImplementedError` without a round trip. `SftpPath.checksum()`
        translates any other failure to `NotImplementedError` too.
        """
        client = self.client(path.source)
        if self._cached_support(client) == frozenset():
            raise NotImplementedError(
                f"{_CHECK_FILE_EXTENSION}: not supported by this server"
            )
        # The extension hashes an *open handle*, not a bare path -- open
        # read-only, always close even on failure so a checksum attempt
        # (whether it succeeds, or the server simply doesn't support the
        # extension) never leaks a file handle.
        handle_file = client.open(path.path, "r")
        try:
            handle = handle_file.handle
            try:
                msg_type, msg = client._request(
                    _paramiko_sftp.CMD_EXTENDED,
                    _CHECK_FILE_EXTENSION,
                    handle,
                    algorithm,  # hash-algorithm-list: just the one wanted
                    # int64(...): a plain `int` would be packed as a 32-bit
                    # int by Message.add() (see paramiko's _async_request
                    # arg-type dispatch) -- the wire format requires uint64
                    # for start-offset/length.
                    _paramiko_sftp.int64(0),  # start-offset
                    _paramiko_sftp.int64(0),  # length: 0 means to end of file
                    0,  # block-size: 0 means one hash over the whole range
                )
            except OSError as error:
                if error.errno is None:
                    # A status reply without an errno: paramiko's rendering
                    # of SSH_FX_OP_UNSUPPORTED (and any other generic
                    # failure) to the extension request itself.
                    self._cache_support(client, frozenset())
                raise
        finally:
            handle_file.close()
        if msg_type != _paramiko_sftp.CMD_EXTENDED_REPLY:
            self._cache_support(client, frozenset())
            raise NotImplementedError(
                f"{_CHECK_FILE_EXTENSION}: unexpected reply type "
                f"{msg_type!r} (server likely doesn't support this extension)"
            )
        reply_algorithm = msg.get_text()
        if reply_algorithm == "check-file":
            # Later filexfer drafts prefix the reply with the extension name.
            reply_algorithm = msg.get_text()
        if reply_algorithm != algorithm:
            # A server MUST echo back one of the algorithms we offered --
            # if it names something else, don't trust the digest.
            raise NotImplementedError(
                f"{_CHECK_FILE_EXTENSION} returned {reply_algorithm!r}, "
                f"requested {algorithm!r}"
            )
        digest = msg.get_remainder()
        try:
            expected = _hashlib.new(algorithm).digest_size
        except ValueError:
            expected = None
        if expected is not None and len(digest) != expected:
            # The hash is the raw rest of the packet; any other length means
            # a reply shape this parser does not understand.
            raise NotImplementedError(
                f"{_CHECK_FILE_EXTENSION} returned a {len(digest)}-byte "
                f"{algorithm} digest, expected {expected}"
            )
        return digest.hex()

    @staticmethod
    def _cached_support(client) -> "frozenset[str] | None":
        with _CHECKSUM_SUPPORT_LOCK:
            try:
                return _CHECKSUM_SUPPORT_CACHE.get(client)
            except TypeError:
                return None

    @staticmethod
    def _cache_support(client, supported: "frozenset[str]") -> None:
        with _CHECKSUM_SUPPORT_LOCK:
            try:
                _CHECKSUM_SUPPORT_CACHE[client] = supported
            except TypeError:
                pass

    def supported_checksums(self, path: "SftpPath") -> "frozenset[str]":
        """Per-connection probe: try `checksum()` against `path` once and
        cache the answer for this connected client (see
        `_CHECKSUM_SUPPORT_CACHE` above -- paramiko has no cheaper way to
        learn this). `path` must already exist and be readable, or the
        probe's own `open()` fails for an unrelated reason (a missing
        file), which is reported as empty here but not cached -- this
        method is advisory, never raises.

        Only the FIRST algorithm is actually attempted: a server either
        implements the extension (and then the draft's algorithm set) or it
        doesn't. `checksum()` itself remains authoritative for any specific
        algorithm and still raises `NotImplementedError` per call regardless
        of what this advertises.
        """
        client = self.client(path.source)
        cached = self._cached_support(client)
        if cached is not None:
            return cached

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

        self._cache_support(client, supported)
        return supported

    @classmethod
    def default(cls, ssh_config=_DEFAULT_SSH_CONFIG) -> "SftpBackend":
        return cls({}, None, ssh_config=ssh_config)
