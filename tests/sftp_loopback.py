"""An in-process asyncssh SFTP server on 127.0.0.1 that counts every request
the client sends, can fail or reshape any of them, and can refuse a login, for
the tests of the sftp: scheme.

Nothing here leaves the machine: the server listens on port 0 of the loopback
interface and every test passes the host key or the known_hosts file it needs
explicitly.
"""

from __future__ import annotations

import asyncio
import collections
import re
import sys
import threading

import asyncssh
from server_loops import stop_server

#: The server-side methods that are counted and can carry a hook.
COUNTED = (
    "stat lstat fstat open open56 close read write remove rmdir mkdir rename "
    "posix_rename setstat lsetstat fsetstat readlink symlink link realpath"
).split()


class Wire:
    """Every request the server received, and what to do about them."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls: "collections.Counter[str]" = collections.Counter()
        self.log: "list[tuple[str, tuple]]" = []
        #: name -> hook(server, *args): runs first; raising fails the request.
        self.before = {}
        #: name -> hook(result, *args) -> result: reshapes the answer.
        self.after = {}
        #: callable(path: bytes) -> extra asyncssh.SFTPName entries a listing
        #: of that directory also holds.
        self.listing = None

    def record(self, name, args=()):
        with self._lock:
            self.calls[name] += 1
            self.log.append((name, args))

    def total(self):
        with self._lock:
            return sum(self.calls.values())

    def count(self, *names):
        with self._lock:
            return sum(self.calls[name] for name in names)

    def snapshot(self):
        with self._lock:
            return dict(self.calls)

    def reset(self):
        with self._lock:
            self.calls.clear()
            self.log.clear()

    def paths(self, name):
        with self._lock:
            return [args[0] for call, args in self.log if call == name and args]


_ESCAPED = re.compile("~([0-9a-f]{2})~")


def name_on_disk(name: bytes) -> bytes:
    """The local file name that stands for the wire name `name`: each byte
    that is not UTF-8 becomes `~xx~`, so a file can be created for it on a
    filesystem (Windows, macOS) that only holds well-formed names."""
    text = name.decode("utf-8", "surrogateescape")
    return "".join(
        f"~{ord(char) - 0xDC00:02x}~" if 0xDC80 <= ord(char) <= 0xDCFF else char
        for char in text
    ).encode("utf-8")


def name_on_wire(name: bytes) -> bytes:
    """The inverse of `name_on_disk`."""
    return _ESCAPED.sub(
        lambda match: chr(0xDC00 + int(match[1], 16)), name.decode("utf-8")
    ).encode("utf-8", "surrogateescape")


class EscapedNamesServer(asyncssh.SFTPServer):
    """Serves a directory whose files carry names that are not UTF-8 on the
    wire, stored under `name_on_disk()` names."""

    def map_path(self, path):
        return super().map_path(name_on_disk(path))

    async def scandir(self, path):
        async for entry in super().scandir(path):
            name = entry.filename
            if isinstance(name, str):
                name = name.encode("utf-8")
            yield asyncssh.SFTPName(name_on_wire(name), entry.longname, entry.attrs)


def counting_server_class(wire, base=asyncssh.SFTPServer):
    class _Server(base):
        pass

    def instrument(name):
        original = getattr(base, name)

        def method(self, *args):
            wire.record(name, args[:1])
            before = wire.before.get(name)
            if before is not None:
                before(self, *args)
            result = original(self, *args)
            after = wire.after.get(name)
            return result if after is None else after(result, *args)

        method.__name__ = name
        return method

    for name in COUNTED:
        if hasattr(base, name):
            setattr(_Server, name, instrument(name))

    original_scandir = base.scandir

    async def scandir(self, path):
        wire.record("scandir", (path,))
        extra = wire.listing(path) if wire.listing else ()
        for entry in extra:
            yield entry
        async for entry in original_scandir(self, path):
            yield entry

    _Server.scandir = scandir
    return _Server


class Loopback:
    """An asyncssh SFTP server on 127.0.0.1 serving `root`, on its own loop.

    `password` makes the login require it (anything else is refused);
    `sftp_version` is the highest version the server speaks; `server_class`
    replaces the counting `SFTPServer` subclass; `escaped_names` serves the
    names `name_on_disk()` stores as the bytes they stand for.
    """

    def __init__(
        self,
        root,
        wire=None,
        *,
        password=None,
        sftp_version=3,
        server_class=None,
        escaped_names=False,
    ):
        self.root = root
        self.wire = Wire() if wire is None else wire
        self.password = password
        self.sftp_version = sftp_version
        self.server_class = server_class
        self.escaped_names = escaped_names
        self.host_key = asyncssh.generate_private_key("ssh-rsa")
        self.live = set()
        #: Connections accepted so far, and the most that were open at once.
        self.connections = 0
        self.open_connections = 0
        self.peak_connections = 0
        self.logins = []
        self.loop = (
            asyncio.SelectorEventLoop()
            if sys.platform == "win32"
            else asyncio.new_event_loop()
        )
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.port = None
        self._server = None
        self._lock = threading.Lock()

    def _call(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self):
        state = self
        server_class = self.server_class or counting_server_class(
            self.wire,
            EscapedNamesServer if self.escaped_names else asyncssh.SFTPServer,
        )
        root = str(self.root)
        password = self.password

        class _Auth(asyncssh.SSHServer):
            def connection_made(self, conn):
                self._conn = conn
                with state._lock:
                    state.connections += 1
                    state.open_connections += 1
                    state.peak_connections = max(
                        state.peak_connections, state.open_connections
                    )
                state.live.add(conn)

            def connection_lost(self, exc):
                with state._lock:
                    state.open_connections -= 1
                state.live.discard(self._conn)

            def begin_auth(self, username):
                return password is not None

            def password_auth_supported(self):
                return password is not None

            def validate_password(self, username, given):
                state.logins.append((username, given))
                return given == password

        async def _listen():
            return await asyncssh.listen(
                "127.0.0.1",
                0,
                server_factory=_Auth,
                server_host_keys=[self.host_key],
                sftp_factory=lambda chan: server_class(chan, chroot=root),
                sftp_version=self.sftp_version,
                process_factory=None,
                # No GSS: its default asks the resolver for this machine's own name.
                gss_host=None,
            )

        self.thread.start()
        self._server = self._call(_listen())
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    def drop_connections(self):
        """Abort every accepted connection, as a crash of the server would.
        Called from a request hook (which runs on the server's own loop) the
        connections are gone before the request is answered."""

        def abort():
            for conn in list(self.live):
                conn.abort()

        if threading.current_thread() is self.thread:
            abort()
        else:
            self.loop.call_soon_threadsafe(abort)

    def stop(self):
        stop_server(self.loop, self.thread, self._server, self.live)

    @property
    def host(self):
        return "127.0.0.1"

    def public_key(self):
        """`(algorithm, base64 key)` of the host key."""
        return tuple(self.host_key.export_public_key("openssh").decode().split()[:2])

    def known_hosts_line(self):
        algorithm, key = self.public_key()
        return f"[{self.host}]:{self.port} {algorithm} {key}\n"

    def url(self, path="", *, user="x", password="x"):
        auth = f"{user}:{password}@" if password is not None else f"{user}@"
        return f"sftp://{auth}{self.host}:{self.port}/{path}"
