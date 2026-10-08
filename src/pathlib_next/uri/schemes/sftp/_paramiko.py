from __future__ import annotations

import errno as _errno
import functools as _functools
import io as _io
import pathlib as _pathlib
import socket as _socket
import threading as _thread

import netimps as _netimps
import paramiko as _paramiko
import paramiko.sftp as _paramiko_sftp
from paramiko.sftp_attr import SFTPAttributes as _SFTPAttributes

from .... import utils as _utils
from ... import Source
from . import _checkfile, _errors

# The sentinel + path normalization are paramiko-free and now live in
# ``_sshconfig`` so the asyncssh backend and the scheme ``__init__`` can use them
# without importing paramiko. Re-exported here for backward compatibility (older
# code did ``from ._paramiko import _DEFAULT_SSH_CONFIG``).
from ._sshconfig import (
    _DEFAULT_SSH_CONFIG,
    _check_host,
    _expand_includes,
    _expand_proxy_command,
    _normalize_config_paths,
)

#: Sentinel for `SftpBackend(known_hosts=...)`: the user's `~/.ssh/known_hosts`
#: plus every `UserKnownHostsFile` the ssh_config names for the host.
_DEFAULT_KNOWN_HOSTS = object()


class _SSHConfig(_paramiko.SSHConfig):
    """paramiko's parser, except that a ProxyCommand comes back with its
    ``%`` tokens unexpanded: ``SftpBackend.opts()`` expands them with the host,
    port and user chosen for the connection, which a lookup by host name
    alone cannot know."""

    TOKENS_BY_CONFIG_KEY = {
        **_paramiko.SSHConfig.TOKENS_BY_CONFIG_KEY,
        "proxycommand": ["~"],
    }


@_utils.LRU
def _load_ssh_config(config_paths: "tuple[str, ...]") -> "_SSHConfig | None":
    config = _SSHConfig()
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


# --- the SFTP client ---------------------------------------------------------


def _wire_path(path):
    """A `str` path as the bytes it stands for on the wire: a lone surrogate is
    a byte of a name that was not UTF-8 when it was listed."""
    return path.encode("utf-8", "surrogateescape") if isinstance(path, str) else path


def _text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


class _SFTPFile(_paramiko.SFTPFile):
    """paramiko's file with the two `io` behaviours it lacks: `write()`
    returns the number of bytes accepted, and `fileno()` says it has none."""

    def write(self, data):
        count = (
            len(data.encode("utf-8"))
            if isinstance(data, str)
            else memoryview(data).nbytes
        )
        super().write(data)
        return count

    def fileno(self):
        raise _io.UnsupportedOperation("fileno")


def _named_errors(method, two_paths: bool = False):
    """`method(self, path, ...)` with the path(s) in the `filename`
    (`filename2`) of the `OSError` a server status raised, as pathlib's own
    errors carry them."""

    @_functools.wraps(method)
    def wrapper(self, path, *args, **kwargs):
        try:
            return method(self, path, *args, **kwargs)
        except OSError as error:
            if error.filename is None and not isinstance(
                error, (ConnectionError, TimeoutError)
            ):
                error.filename = path if isinstance(path, str) else _text(path)
                if two_paths and args:
                    error.filename2 = args[0]
            raise

    return wrapper


class _SFTPClient(_paramiko.SFTPClient):
    """paramiko's client with the status mapping and the names the asyncssh
    backend has: an "operation unsupported" status is `NotImplementedError`,
    a lost connection `ConnectionResetError`, an error carries the path it
    was about, and a name that is not UTF-8 lists, opens and removes as the
    lone-surrogate string of its bytes."""

    def _convert_status(self, msg):
        code = msg.get_int()
        text = msg.get_text()
        if code == _paramiko_sftp.SFTP_OK:
            return
        if code == _paramiko_sftp.SFTP_EOF:
            raise EOFError(text)
        if code == _paramiko_sftp.SFTP_NO_SUCH_FILE:
            raise IOError(_errno.ENOENT, text)
        if code == _paramiko_sftp.SFTP_PERMISSION_DENIED:
            raise IOError(_errno.EACCES, text)
        if code == _paramiko_sftp.SFTP_OP_UNSUPPORTED:
            raise NotImplementedError(text)
        if code in (
            _paramiko_sftp.SFTP_NO_CONNECTION,
            _paramiko_sftp.SFTP_CONNECTION_LOST,
        ):
            raise _errors.lost()
        raise IOError(text)

    def _read_response(self, waitfor=None):
        try:
            return super()._read_response(waitfor)
        except _paramiko.SSHException:
            # "Server connection dropped": the only SSHException it raises.
            raise _errors.lost() from None

    def _send_packet(self, t, packet):
        try:
            super()._send_packet(t, packet)
        except (EOFError, _paramiko.SSHException):
            raise _errors.lost() from None
        except OSError as error:
            if error.errno is not None:
                raise
            # A closed channel: "Socket is closed".
            raise _errors.lost() from None

    def _adjust_cwd(self, path):
        return super()._adjust_cwd(_wire_path(path))

    def symlink(self, source, dest):
        return super().symlink(_wire_path(source), dest)

    def listdir_attr(self, path="."):
        # paramiko decodes every listed name as strict UTF-8, so one other
        # name fails the whole listing.
        path = self._adjust_cwd(path)
        kind, msg = self._request(_paramiko_sftp.CMD_OPENDIR, path)
        if kind != _paramiko_sftp.CMD_HANDLE:
            raise _paramiko.SFTPError("Expected handle")
        handle = msg.get_binary()
        entries = []
        while True:
            try:
                kind, msg = self._request(_paramiko_sftp.CMD_READDIR, handle)
            except EOFError:
                break
            if kind != _paramiko_sftp.CMD_NAME:
                raise _paramiko.SFTPError("Expected name response")
            for _ in range(msg.get_int()):
                filename = _text(msg.get_string())
                longname = _text(msg.get_string())
                attrs = _SFTPAttributes._from_msg(msg, filename, longname)
                if filename not in (".", ".."):
                    entries.append(attrs)
        self._request(_paramiko_sftp.CMD_CLOSE, handle)
        return entries

    def readlink(self, path):
        path = self._adjust_cwd(path)
        kind, msg = self._request(_paramiko_sftp.CMD_READLINK, path)
        if kind != _paramiko_sftp.CMD_NAME:
            raise _paramiko.SFTPError("Expected name response")
        count = msg.get_int()
        if count == 0:
            return None
        if count != 1:
            raise _paramiko.SFTPError(f"Readlink returned {count} results")
        return _text(msg.get_string())

    def open(self, filename, mode="r", bufsize=-1):
        file = super().open(filename, mode, bufsize)
        # The same attributes and slots: only `write()` and `fileno()` differ.
        file.__class__ = _SFTPFile
        return file


for (
    _name
) in "stat lstat listdir_attr open mkdir chmod chown remove rmdir readlink".split():
    setattr(_SFTPClient, _name, _named_errors(getattr(_SFTPClient, _name)))
for _name in "rename posix_rename symlink".split():
    setattr(_SFTPClient, _name, _named_errors(getattr(_SFTPClient, _name), True))
del _name


class _NoTransport(_paramiko.SSHException):
    """`connect()` returned without a transport: a broken invariant, not a
    failure of the connection, so it is raised as it is."""


def _timed_out(error: BaseException) -> bool:
    """Whether `error`, or what it was raised while handling, is a socket
    timeout: paramiko re-raises one as an `SSHException` ("Error reading SSH
    protocol banner") or words its own ("Authentication timeout")."""
    seen = 0
    while error is not None and seen < 8:
        if isinstance(error, _socket.timeout):
            return True
        error = error.__cause__ or error.__context__
        seen += 1
    return False


def _connect_error(error: BaseException, source: Source) -> "Exception | None":
    """The `OSError` for a paramiko failure to connect, log in or open the
    SFTP session, or None when `error` is not one of those."""
    if isinstance(error, _NoTransport):
        return None
    if isinstance(error, _paramiko.ssh_exception.NoValidConnectionsError):
        # Every address refused: say what the first one said, as asyncssh does.
        for cause in error.errors.values():
            if isinstance(cause, OSError) and cause.errno is not None:
                return OSError(cause.errno, cause.strerror)
        return None
    if isinstance(error, _paramiko.BadHostKeyException):
        return _errors.host_key_refused(source)
    if isinstance(error, _paramiko.SSHException):
        text = str(error)
        if _timed_out(error) or "timeout" in text.lower():
            return TimeoutError(_errno.ETIMEDOUT, "SFTP connection timed out")
        # `RejectPolicy` (the default) refuses a key no known_hosts file has.
        if "not found in known_hosts" in text:
            return _errors.host_key_refused(source)
        # "No authentication methods available": nothing was left to try.
        if (
            isinstance(error, _paramiko.AuthenticationException)
            or "No authentication methods" in text
        ):
            return _errors.login_refused(source)
        return _errors.aborted(error)
    if isinstance(error, EOFError):
        return _errors.lost()
    if isinstance(error, _socket.timeout) and not isinstance(error, TimeoutError):
        # Python 3.9: socket.timeout is not yet TimeoutError.
        return TimeoutError(_errno.ETIMEDOUT, "SFTP connection timed out")
    return None


def _create_sftpclient(backend: "SftpBackend", source: Source, thread_id: int):
    transport = backend.transport(source)
    try:
        client = transport.open_sftp_client()
        if client is None:
            raise _paramiko.SSHException("the server refused the sftp subsystem")
        if type(client) is _paramiko.SFTPClient:
            # `_SFTPClient` adds behaviour and no state.
            client.__class__ = _SFTPClient
        return client
    except BaseException as error:
        transport.close()
        translated = _connect_error(error, source)
        if translated is None:
            raise
        raise translated from None


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


class SftpBackend(_checkfile.CheckFileSftpBackend):
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
        _check_host(source.host)
        host = str(source.host)
        config = _lookup_ssh_config(host, self.ssh_config)
        connect_ops = {
            **self.connect_opts,
            "hostname": config.get("hostname", host),
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
                    _expand_proxy_command(
                        str(config["proxycommand"]),
                        host=host,
                        hostname=str(connect_ops["hostname"]),
                        port=connect_ops["port"],
                        user=connect_ops.get("username"),
                    )
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
        _check_host(source.host)
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
                raise _NoTransport("connect() produced no transport")
        except BaseException as error:
            # A failed connect/auth still leaves a running Transport thread
            # and an open socket (or a ProxyCommand subprocess) behind.
            client.close()
            sock = opts.get("sock")
            if sock is not None and "sock" not in self.connect_opts:
                try:
                    sock.close()
                except Exception:
                    pass
            translated = _connect_error(error, source)
            if translated is None:
                raise
            raise translated from None
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

    def _check_file_request(self, client, file, algorithm: str) -> bytes:
        # Not part of paramiko's public API -- the same low-level
        # `_request(CMD_EXTENDED, ...)` primitive paramiko itself uses for
        # `posix-rename@openssh.com`. A failure status arrives as OSError,
        # without an errno for SSH_FX_OP_UNSUPPORTED.
        msg_type, msg = client._request(
            _paramiko_sftp.CMD_EXTENDED,
            _checkfile.EXTENSION,
            file.handle,
            algorithm,  # hash-algorithm-list: just the one wanted
            # int64(...): a plain `int` would be packed as a 32-bit int by
            # Message.add() (see paramiko's _async_request arg-type
            # dispatch) -- the wire format requires uint64 for
            # start-offset/length.
            _paramiko_sftp.int64(_checkfile.START_OFFSET),
            _paramiko_sftp.int64(_checkfile.LENGTH),
            _checkfile.BLOCK_SIZE,
        )
        if msg_type != _paramiko_sftp.CMD_EXTENDED_REPLY:
            raise NotImplementedError(
                f"{_checkfile.EXTENSION}: unexpected reply type "
                f"{msg_type!r} (server likely doesn't support this extension)"
            )
        return msg.get_remainder()

    @classmethod
    def default(cls, ssh_config=_DEFAULT_SSH_CONFIG) -> "SftpBackend":
        return cls({}, None, ssh_config=ssh_config)
