"""SFTP transport security and connection lifecycle, against a loopback
asyncssh server (never an external host).

Covers the 2026-09-15 review's wave-3 SFTP findings: host keys verified by
default on both backends with an explicit in-code opt-out, bounded paramiko
connect timeouts, asyncssh `_run()` request-vs-whole-operation timeouts and
bridge-loop re-entry, dead/evicted/failed connections closed and replaced,
the asyncssh connection-cache race, open-handle capping during recursive
copy, `ssh_config` capture on direct construction, paramiko `Include` /
`ProxyJump`, and `utils.LRU`'s eviction callback. Assertions are on what the
server saw (credentials, live connections) or what the backend received.
"""

import asyncio
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

asyncssh = pytest.importorskip("asyncssh")

from pathlib_next import utils  # noqa: E402
from pathlib_next.uri import Source  # noqa: E402
from pathlib_next.uri.schemes import sftp as sftp_pkg  # noqa: E402
from pathlib_next.uri.schemes.sftp import _asyncssh as backend_mod  # noqa: E402
from pathlib_next.uri.schemes.sftp import SftpPath  # noqa: E402
from pathlib_next.uri.schemes.sftp._sshconfig import _DEFAULT_SSH_CONFIG  # noqa: E402

try:
    import paramiko
except ImportError:  # asyncssh-only install
    paramiko = None

needs_paramiko = pytest.mark.skipif(paramiko is None, reason="paramiko not installed")

_LOOP_THREAD_NAME = "pathlib_next-asyncssh-loop"


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --- loopback server ----------------------------------------------------------


class _LoopbackServer:
    """asyncssh SFTP server on 127.0.0.1 that records what clients did:
    every password offered, connections accepted and still open."""

    def __init__(self, root, *, password=None, sftp_server_cls=None):
        self.root = root
        self.password = password
        self.sftp_server_cls = sftp_server_cls or asyncssh.SFTPServer
        self.host_key = asyncssh.generate_private_key("ssh-rsa")
        self.credentials = []
        self.live = set()
        self.accepted = 0
        self.loop = (
            asyncio.SelectorEventLoop()
            if sys.platform == "win32"
            else asyncio.new_event_loop()
        )
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._server = None
        self.port = None

    def _call(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self):
        state = self

        class _Server(asyncssh.SSHServer):
            def connection_made(self, conn):
                self._conn = conn
                state.live.add(conn)
                state.accepted += 1

            def connection_lost(self, exc):
                state.live.discard(self._conn)

            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                state.credentials.append((username, password))
                return state.password is None or password == state.password

        root = str(self.root)

        async def _start():
            return await asyncssh.listen(
                "127.0.0.1",
                0,
                server_factory=_Server,
                server_host_keys=[self.host_key],
                sftp_factory=lambda chan: self.sftp_server_cls(chan, chroot=root),
                process_factory=None,
            )

        self.thread.start()
        self._server = self._call(_start())
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    def stop(self):
        async def _shutdown():
            self._server.close()
            for conn in list(self.live):
                conn.abort()
            await asyncio.sleep(0.05)

        try:
            self._call(_shutdown(), timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)

    def drop_connections(self):
        async def _drop():
            for conn in list(self.live):
                conn.abort()

        self._call(_drop())

    def url(self, path="", user="alice", password="s3cret"):
        return f"sftp://{user}:{password}@127.0.0.1:{self.port}/{path}"

    def source(self, user="alice", password="s3cret"):
        return Source("sftp", f"{user}:{password}", "127.0.0.1", self.port)

    def known_hosts_line(self, key=None):
        key = key or self.host_key
        kind, blob = key.export_public_key("openssh").decode().split()[:2]
        return f"[127.0.0.1]:{self.port} {kind} {blob}\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def server_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "hello.txt").write_text("hello")
    return root


@pytest.fixture
def server(server_root, home):
    srv = _LoopbackServer(server_root).start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def backends():
    """Close every backend a test opened, even when it fails."""
    opened = []
    yield opened
    for backend in opened:
        backend.close()


def _asyncssh_backend(backends, **kwargs):
    backend = backend_mod.AsyncsshSftpBackend(**kwargs)
    backends.append(backend)
    return backend


def _paramiko_backend(backends, connect_opts=None, *args, **kwargs):
    opts = {"allow_agent": False, "look_for_keys": False}
    opts.update(connect_opts or {})
    backend = sftp_pkg.SftpBackend(opts, *args, **kwargs)
    backends.append(backend)
    return backend


# --- host keys: verified by default, explicit opt-out ---------------------------


def test_asyncssh_rejects_unknown_host_key_by_default(server, backends):
    backend = _asyncssh_backend(backends)
    with pytest.raises(asyncssh.HostKeyNotVerifiable):
        SftpPath(server.url("hello.txt"), backend=backend).read_text()
    # Rejected before authentication: the password never reached the server.
    assert server.credentials == []


def test_asyncssh_accepts_unknown_host_key_with_explicit_opt_out(server, backends):
    backend = _asyncssh_backend(backends, connect_opts={"known_hosts": None})
    assert SftpPath(server.url("hello.txt"), backend=backend).read_text() == "hello"
    assert server.credentials == [("alice", "s3cret")]


def test_asyncssh_verifies_against_user_known_hosts(server, home, backends):
    (home / ".ssh" / "known_hosts").write_text(server.known_hosts_line())
    backend = _asyncssh_backend(backends)
    assert SftpPath(server.url("hello.txt"), backend=backend).read_text() == "hello"


@needs_paramiko
def test_paramiko_rejects_unknown_host_key_by_default(server, backends):
    backend = _paramiko_backend(backends)
    assert isinstance(backend.hostkeypolicy, paramiko.RejectPolicy)
    with pytest.raises(paramiko.SSHException, match="not found in known_hosts"):
        SftpPath(server.url("hello.txt"), backend=backend).read_text()
    assert server.credentials == []
    # The refused connection is closed, not left to a live Transport thread.
    assert _wait_until(lambda: not server.live)


@needs_paramiko
def test_paramiko_default_backend_rejects_unknown_host_keys():
    backend = sftp_pkg.SftpBackend.default()
    assert isinstance(backend.hostkeypolicy, paramiko.RejectPolicy)
    assert backend.known_hosts is sftp_pkg._paramiko._DEFAULT_KNOWN_HOSTS


@needs_paramiko
def test_paramiko_accepts_unknown_host_key_with_explicit_opt_out(
    server, home, backends
):
    backend = _paramiko_backend(
        backends, None, paramiko.AutoAddPolicy(), known_hosts=None
    )
    assert SftpPath(server.url("hello.txt"), backend=backend).read_text() == "hello"
    assert server.credentials == [("alice", "s3cret")]
    # Host keys are loaded read-only: nothing was written to the user's files.
    assert not (home / ".ssh" / "known_hosts").exists()


@needs_paramiko
def test_paramiko_changed_host_key_is_rejected_even_with_autoadd(
    server, home, backends
):
    other_key = asyncssh.generate_private_key("ssh-rsa")
    (home / ".ssh" / "known_hosts").write_text(server.known_hosts_line(other_key))
    backend = _paramiko_backend(backends, None, paramiko.AutoAddPolicy())
    with pytest.raises(paramiko.BadHostKeyException):
        SftpPath(server.url("hello.txt"), backend=backend).read_text()
    assert server.credentials == []


@needs_paramiko
def test_paramiko_trusts_ssh_config_user_known_hosts_file(server, home, backends):
    (home / "pinned_hosts").write_text(server.known_hosts_line())
    config = home / ".ssh" / "config"
    config.write_text("Host 127.0.0.1\n  UserKnownHostsFile ~/pinned_hosts\n")
    backend = _paramiko_backend(backends, None, None, ssh_config=str(config))
    assert SftpPath(server.url("hello.txt"), backend=backend).read_text() == "hello"


# --- timeouts --------------------------------------------------------------------


@needs_paramiko
def test_paramiko_connect_timeouts_default_to_30s_and_are_overridable():
    source = Source("sftp", None, "host", 22)
    opts = sftp_pkg.SftpBackend(ssh_config=None).opts(source)
    for key in ("timeout", "banner_timeout", "auth_timeout", "channel_timeout"):
        assert opts[key] == 30.0
    opts = sftp_pkg.SftpBackend({"banner_timeout": 7}, ssh_config=None, timeout=3).opts(
        source
    )
    assert opts["banner_timeout"] == 7
    assert opts["timeout"] == opts["auth_timeout"] == 3
    opts = sftp_pkg.SftpBackend(ssh_config=None, timeout=None).opts(source)
    assert "timeout" not in opts and "banner_timeout" not in opts


@needs_paramiko
def test_paramiko_connect_to_silent_server_times_out(home, backends):
    # Accepts TCP and never sends an SSH banner: without a bound, connect()
    # blocks forever.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted = []
    acceptor = threading.Thread(
        target=lambda: accepted.append(listener.accept()), daemon=True
    )
    acceptor.start()
    port = listener.getsockname()[1]
    try:
        backend = _paramiko_backend(
            backends, None, paramiko.AutoAddPolicy(), known_hosts=None, timeout=0.5
        )
        start = time.monotonic()
        with pytest.raises(paramiko.SSHException):
            backend.client(Source("sftp", "a:b", "127.0.0.1", port))
        assert time.monotonic() - start < 10
    finally:
        listener.close()
        for conn, _addr in accepted:
            conn.close()


def test_run_timeout_cancels_request_and_raises_builtin_timeouterror():
    cancelled = threading.Event()

    async def _stalled():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError) as excinfo:
        backend_mod._run(_stalled(), 0.2)
    # The builtin, on every Python version (concurrent.futures.TimeoutError
    # is a different class before 3.11).
    assert type(excinfo.value) is TimeoutError
    # The request was cancelled on the loop, not left running.
    assert cancelled.wait(5)


def test_run_on_bridge_loop_thread_raises_instead_of_deadlocking():
    async def _reenter():
        start = time.monotonic()
        try:
            backend_mod._run(asyncio.sleep(0), 30)
        except RuntimeError as error:
            return error, threading.current_thread().name, time.monotonic() - start
        return None, None, None

    error, thread_name, elapsed = backend_mod._run(_reenter(), 10)
    assert isinstance(error, RuntimeError)
    assert thread_name == _LOOP_THREAD_NAME
    assert elapsed < 1


class _SlowAsyncFile:
    def __init__(self, delay):
        self.delay = delay

    async def read(self, size=-1):
        await asyncio.sleep(self.delay)
        return b"payload"

    async def write(self, data):
        await asyncio.sleep(self.delay)
        return len(data)

    async def seek(self, offset, whence=0):
        await asyncio.sleep(self.delay)
        return offset

    async def close(self):
        pass


def test_streaming_reads_and_writes_are_not_bounded_by_request_timeout():
    sync_file = backend_mod._SyncSftpFile(_SlowAsyncFile(0.5), timeout=0.1)
    assert sync_file.read() == b"payload"
    assert sync_file.write(b"abc") == 3
    # A single request on the same file is bounded.
    with pytest.raises(TimeoutError):
        sync_file.seek(0)
    sync_file.close()


def test_single_client_request_is_bounded_by_backend_timeout():
    class _SlowClient:
        async def stat(self, path):
            await asyncio.sleep(1)

    client = backend_mod._SyncSftpClient(_SlowClient(), timeout=0.1)
    with pytest.raises(TimeoutError):
        client.stat("/x")


def test_asyncssh_backend_timeout_reaches_the_sync_client(server, backends):
    backend = _asyncssh_backend(backends, connect_opts={"known_hosts": None}, timeout=7)
    assert backend.client(server.source())._timeout == 7


# --- asyncssh recursive rm/copy: connection resolved off the loop ---------------


def test_asyncssh_rm_recursive_as_first_call_on_a_fresh_backend(
    server, server_root, backends
):
    tree = server_root / "tree"
    (tree / "sub").mkdir(parents=True)
    for name in ("a.txt", "sub/b.txt", "sub/c.txt"):
        (tree / name).write_text(name)
    backend = _asyncssh_backend(backends, connect_opts={"known_hosts": None})
    start = time.monotonic()
    SftpPath(server.url("tree"), backend=backend).rm(recursive=True)
    assert time.monotonic() - start < 10
    assert not tree.exists()


def test_asyncssh_copy_recursive_as_first_call_on_a_fresh_backend(
    server, server_root, backends
):
    (server_root / "src" / "sub").mkdir(parents=True)
    (server_root / "src" / "sub" / "x.txt").write_text("x")
    backend = _asyncssh_backend(backends, connect_opts={"known_hosts": None})
    src = SftpPath(server.url("src"), backend=backend)
    src.copy(SftpPath(server.url("dst"), backend=backend), recursive=True)
    assert (server_root / "dst" / "sub" / "x.txt").read_text() == "x"


class _LockedRemoveServer(asyncssh.SFTPServer):
    def remove(self, path):
        if path.endswith(b"locked.txt"):
            raise asyncssh.SFTPPermissionDenied("locked")
        return super().remove(path)


def test_rm_ignore_error_callback_may_call_sync_path_methods(
    server_root, home, backends
):
    tree = server_root / "tree"
    tree.mkdir()
    (tree / "locked.txt").write_text("keep")
    (tree / "ok.txt").write_text("go")
    srv = _LoopbackServer(server_root, sftp_server_cls=_LockedRemoveServer).start()
    try:
        backend = _asyncssh_backend(backends, connect_opts={"known_hosts": None})
        seen = []

        def _ignore_if_gone(error, path):
            # A sync Path method: before the fix this ran on the bridge loop
            # and blocked it until the request timeout.
            seen.append((path.name, path.exists(), threading.current_thread().name))
            return True

        start = time.monotonic()
        SftpPath(srv.url("tree"), backend=backend).rm(
            recursive=True, ignore_error=_ignore_if_gone
        )
        assert time.monotonic() - start < 10
    finally:
        srv.stop()
    assert (tree / "locked.txt").exists()
    assert not (tree / "ok.txt").exists()
    assert ("locked.txt", True) in [(name, exists) for name, exists, _ in seen]
    assert all(thread != _LOOP_THREAD_NAME for _, _, thread in seen)


class _HandleCountingFile:
    def __init__(self, client, path, mode):
        self.client, self.path, self.mode = client, path, mode
        self.done = False

    async def read(self, size=-1):
        await asyncio.sleep(0)
        if self.done:
            return b""
        self.done = True
        return self.client.files[self.path]

    async def write(self, data):
        await asyncio.sleep(0)
        self.client.files[self.path] = data

    async def close(self):
        self.client.open_now -= 1


class _HandleCountingClient:
    def __init__(self, count):
        self.files = {f"/src/f{i}.txt": b"x" for i in range(count)}
        self.open_now = 0
        self.peak = 0

    async def stat(self, path):
        await asyncio.sleep(0)
        if path == "/src":
            return asyncssh.SFTPAttrs(permissions=0o40755)
        if path in self.files:
            return asyncssh.SFTPAttrs(permissions=0o100644)
        raise asyncssh.SFTPNoSuchFile(path)

    lstat = stat

    async def readdir(self, path):
        names = [p.rsplit("/", 1)[1] for p in self.files if p.startswith("/src/")]
        return [SimpleNamespace(filename=name) for name in names]

    async def open(self, path, mode, encoding=None):
        await asyncio.sleep(0)
        self.open_now += 1
        self.peak = max(self.peak, self.open_now)
        return _HandleCountingFile(self, path, mode)

    async def chmod(self, path, mode):
        pass


class _FakePath:
    def __init__(self, path):
        self.path = path
        self.name = path.rsplit("/", 1)[1]

    def __truediv__(self, name):
        return _FakePath(f"{self.path}/{name}")


def test_concurrent_copy_caps_open_handles_at_max_concurrency():
    client = _HandleCountingClient(60)
    asyncio.run(
        backend_mod._concurrent_copy(
            _FakePath("/src"),
            _FakePath("/dst"),
            overwrite=False,
            follow_symlinks=True,
            preserve_metadata=False,
            max_concurrency=4,
            ignore_error=None,
            aclient=client,
        )
    )
    assert sum(1 for path in client.files if path.startswith("/dst/")) == 60
    # Two handles (source + destination) per file being copied; before the
    # fix every queued file opened both first (peak 120 here).
    assert client.peak <= 2 * 4
    assert client.open_now == 0


# --- dead, evicted and failed connections ---------------------------------------


@needs_paramiko
def test_paramiko_reconnects_after_the_server_drops_the_connection(server, backends):
    backend = _paramiko_backend(
        backends, None, paramiko.AutoAddPolicy(), known_hosts=None
    )
    path = SftpPath(server.url("hello.txt"), backend=backend)
    assert path.read_text() == "hello"
    old_client = backend.client(path.source)
    old_transport = old_client.sock.get_transport()

    server.drop_connections()
    assert _wait_until(lambda: not old_transport.is_active())

    assert path.read_text() == "hello"
    assert path.exists()
    assert backend.client(path.source) is not old_client


def test_asyncssh_reconnects_after_the_sftp_channel_closes(server, backends):
    backend = _asyncssh_backend(backends, connect_opts={"known_hosts": None})
    path = SftpPath(server.url("hello.txt"), backend=backend)
    assert path.read_text() == "hello"
    old_entry = backend_mod._CACHE._entries[(backend, path.source)]

    async def _close_channel():
        old_entry.client._aclient.exit()
        await old_entry.client._aclient.wait_closed()

    backend_mod._run(_close_channel(), 10)
    # Only the SFTP channel is gone; the SSH connection is still up.
    assert not old_entry.conn.is_closed()

    assert path.read_text() == "hello"
    assert backend.client(path.source) is not old_entry.client
    # The stale connection was closed, not orphaned.
    assert _wait_until(lambda: old_entry.conn.is_closed())
    assert _wait_until(lambda: len(server.live) == 1)


@needs_paramiko
def test_paramiko_evicted_connections_are_closed(server, backends, monkeypatch):
    """One cache slot, five distinct keys: each new client evicts the
    previous one, whose connection must be closed rather than orphaned.

    The keys come from five backends, not five threads: `threading` reuses
    a finished thread's `get_ident()` value on POSIX, so sequential threads
    share one cache key there and only one connection is ever opened.
    """
    from pathlib_next.uri.schemes.sftp._paramiko import _CACHED_CLIENTS

    monkeypatch.setattr(_CACHED_CLIENTS, "maxsize", 1)
    for _ in range(5):
        backend = _paramiko_backend(
            backends, None, paramiko.AutoAddPolicy(), known_hosts=None
        )
        assert SftpPath(server.url("hello.txt"), backend=backend).read_text() == "hello"

    assert server.accepted == 5
    # One cache slot: every evicted client's connection was closed.
    assert _wait_until(lambda: len(server.live) <= 1)
    for backend in list(backends):
        backend.close()
    assert _wait_until(lambda: not server.live)


@needs_paramiko
def test_paramiko_client_is_reused_by_a_later_thread(server, backends):
    """A cached client outlives the thread that opened it: paramiko's
    transport runs on its own thread, so a later thread reusing the entry
    (the same `get_ident()` value, which POSIX recycles) must get a working
    client, not a dead one -- one connection, both reads served."""
    backend = _paramiko_backend(
        backends, None, paramiko.AutoAddPolicy(), known_hosts=None
    )
    path = SftpPath(server.url("hello.txt"), backend=backend)
    read = []
    for _ in range(2):
        worker = threading.Thread(target=lambda: read.append(path.read_text()))
        worker.start()
        worker.join(30)

    assert read == ["hello", "hello"]


@needs_paramiko
def test_paramiko_failed_login_closes_the_connection(server_root, home, backends):
    srv = _LoopbackServer(server_root, password="right").start()
    try:

        def _transports():
            # The transport threads themselves, not a count: another test's
            # connection may still be shutting down, and comparing totals
            # then fails on whichever side wins the race (measured on the
            # macOS 3.9 runner, on a commit whose earlier run was green).
            return {
                t for t in threading.enumerate() if isinstance(t, paramiko.Transport)
            }

        before = _transports()
        backend = _paramiko_backend(
            backends, None, paramiko.AutoAddPolicy(), known_hosts=None
        )
        for _ in range(3):
            with pytest.raises(paramiko.AuthenticationException):
                SftpPath(srv.url("hello.txt"), backend=backend).read_text()
        assert srv.accepted == 3
        assert _wait_until(lambda: not srv.live)
        # Every transport this test opened is gone; older ones are not ours.
        assert _wait_until(lambda: not (_transports() - before), timeout=30)
    finally:
        srv.stop()


def test_asyncssh_aconnect_closes_connection_when_sftp_start_fails(monkeypatch):
    closed = []

    class _FakeConn:
        async def start_sftp_client(self, *, sftp_version):
            raise asyncssh.ChannelOpenError(1, "no sftp subsystem")

        def close(self):
            closed.append(True)

    async def _fake_connect(host, port, **kwargs):
        return _FakeConn()

    monkeypatch.setattr(backend_mod._asyncssh, "connect", _fake_connect)
    with pytest.raises(asyncssh.ChannelOpenError):
        asyncio.run(backend_mod._aconnect(Source("sftp", None, "host", 22)))
    assert closed == [True]


def test_asyncssh_backend_close_closes_its_connections(server, backends):
    backend = _asyncssh_backend(backends, connect_opts={"known_hosts": None})
    assert SftpPath(server.url("hello.txt"), backend=backend).read_text() == "hello"
    assert _wait_until(lambda: len(server.live) == 1)
    backend.close()
    assert _wait_until(lambda: not server.live)
    assert not [key for key in backend_mod._CACHE._entries if key[0] is backend]


def test_asyncssh_concurrent_first_calls_share_one_connection(server, backends):
    backend = _asyncssh_backend(backends, connect_opts={"known_hosts": None})
    source = server.source()
    barrier = threading.Barrier(8)
    clients = []

    def _connect():
        barrier.wait()
        clients.append(backend.client(source))

    workers = [threading.Thread(target=_connect) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)

    assert len(clients) == 8
    assert len({id(client) for client in clients}) == 1
    assert server.accepted == 1
    assert [key for key in backend_mod._CACHE._entries if key[0] is backend] == [
        (backend, source)
    ]


# --- paramiko-only install: copy() must not need asyncssh -----------------------


@needs_paramiko
def test_copy_works_without_asyncssh_installed():
    import subprocess
    import textwrap

    script = textwrap.dedent("""
        import io, stat, sys
        sys.modules["asyncssh"] = None  # a paramiko-only ('sftp' extra) install
        from pathlib_next.mempath import MemPath
        from pathlib_next.uri.schemes import sftp as pkg
        from pathlib_next.utils.stat import FileStat

        class Client:
            def stat(self, path):
                return FileStat(st_mode=stat.S_IFREG | 0o644, st_size=5)
            lstat = stat
            def open(self, path, mode="r", buffering=-1):
                return io.BytesIO(b"hello")

        class Backend(pkg.BaseSftpBackend):
            def client(self, source):
                return Client()

        assert pkg._resolve_default_backend_cls(reload=True).__name__ == "SftpBackend"
        target = MemPath("/local.txt")
        pkg.SftpPath("sftp://host/remote.txt", backend=Backend()).copy(target)
        assert target.read_bytes() == b"hello", target.read_bytes()
        print("OK")
        """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# --- ssh_config: direct construction, Include, ProxyJump -------------------------


class _RecordingBackend(sftp_pkg.BaseSftpBackend):
    def __init__(self, ssh_config):
        self.ssh_config = ssh_config

    @classmethod
    def default(cls, ssh_config=_DEFAULT_SSH_CONFIG):
        return cls(ssh_config)


class _RecordingSftpPath(SftpPath):
    _default_backend_cls = _RecordingBackend
    __SCHEMES = ()


def test_direct_construction_keeps_the_ssh_config_argument():
    url = "sftp://host/dir/x.txt"
    assert _RecordingSftpPath(url).backend.ssh_config is _DEFAULT_SSH_CONFIG
    assert _RecordingSftpPath(url, ssh_config="/cfg").backend.ssh_config == "/cfg"
    assert _RecordingSftpPath(url, ssh_config=None).backend.ssh_config is None

    parsed_first = _RecordingSftpPath(url, ssh_config="/cfg")
    assert parsed_first.path == "/dir/x.txt"  # lazy parse runs _init() first
    assert parsed_first.backend.ssh_config == "/cfg"

    # Derived paths carry it, like the backend.
    base = _RecordingSftpPath(url, ssh_config="/cfg")
    assert (base / "child")._ssh_config == "/cfg"
    assert base.parent._ssh_config == "/cfg"
    assert _RecordingSftpPath("sftp://other/y", ssh_config="/cfg")._ssh_config == "/cfg"


@needs_paramiko
def test_paramiko_ssh_config_honours_include(tmp_path, home):
    (home / ".ssh" / "inc.conf").write_text(
        "Host myalias\n  HostName 10.9.8.7\n  Port 2222\n  User deploy\n"
    )
    extra = tmp_path / "conf.d"
    extra.mkdir()
    (extra / "10-jump.conf").write_text("Host viaglob\n  HostName 10.1.2.3\n")
    config = home / ".ssh" / "config"
    config.write_text(
        "Include inc.conf\n"
        f"Include {extra.as_posix()}/*.conf\n"
        "Host scoped\n"
        "  Include scoped.conf\n"
        "  User outer\n"
    )
    # Lines after an Include stay in the block that contains it.
    (home / ".ssh" / "scoped.conf").write_text("Port 2200\nHost unrelated\n  User x\n")
    backend = sftp_pkg.SftpBackend(ssh_config=str(config))

    opts = backend.opts(Source("sftp", None, "myalias", None))
    assert (opts["hostname"], opts["port"], opts["username"]) == (
        "10.9.8.7",
        2222,
        "deploy",
    )
    assert backend.opts(Source("sftp", None, "viaglob", None))["hostname"] == "10.1.2.3"
    scoped = backend.opts(Source("sftp", None, "scoped", None))
    assert (scoped["port"], scoped["username"]) == (2200, "outer")


@needs_paramiko
def test_paramiko_ssh_config_proxyjump_fails_loudly(home):
    config = home / ".ssh" / "config"
    config.write_text("Host jumped\n  HostName 10.1.1.1\n  ProxyJump bastion\n")
    source = Source("sftp", None, "jumped", None)
    with pytest.raises(NotImplementedError, match="ProxyJump"):
        sftp_pkg.SftpBackend(ssh_config=str(config)).opts(source)
    # An explicit sock is the caller routing the connection themselves.
    sock = object()
    opts = sftp_pkg.SftpBackend({"sock": sock}, ssh_config=str(config)).opts(source)
    assert opts["sock"] is sock


# --- utils.LRU eviction callback ---------------------------------------------------


def test_lru_on_evict_receives_every_dropped_value():
    evicted = []
    lru = utils.LRU(
        lambda x: [x], maxsize=2, on_evict=lambda k, v: evicted.append((k, v))
    )
    one, two = lru(1), lru(2)
    three = lru(3)  # overflow drops the oldest
    assert evicted == [((1,), one)]
    assert lru.discard(2) is True and lru.discard(2) is False
    assert evicted[-1] == ((2,), two)
    lru(4)
    lru.maxsize = 1  # shrink
    assert evicted[-1] == ((3,), three)
    replaced = lru(4)
    assert lru.invalidate(4) is not replaced
    assert evicted[-1] == ((4,), replaced)


def test_lru_on_evict_errors_do_not_break_lookups():
    def _explode(key, value):
        raise OSError("close failed")

    lru = utils.LRU(lambda x: x * 2, maxsize=1, on_evict=_explode)
    assert lru(1) == 2
    assert lru(2) == 4
    assert lru.invalidate(2) == 4


def test_lru_concurrent_misses_share_the_first_result_and_evict_the_rest():
    barrier = threading.Barrier(4)
    evicted = []
    lru = utils.LRU(
        lambda key: (barrier.wait(), object())[1],
        on_evict=lambda k, v: evicted.append(v),
    )
    results = []
    workers = [
        threading.Thread(target=lambda: results.append(lru("k"))) for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert len(results) == 4 and len({id(r) for r in results}) == 1
    assert len(evicted) == 3 and results[0] not in evicted
