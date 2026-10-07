"""The asyncssh backend's native recursive copy() and rm(), against a loopback
server that counts every request and can fail or reshape any of them.

What a failed, interrupted or refused operation leaves behind must not depend
on which backend ran it, so the scenarios run on both backends and compare
what the server holds afterwards. The asyncssh-only tests pin that a call
which has raised has stopped: the server's request counter does not move after
the exception reached the caller.
"""

import asyncio
import collections
import concurrent.futures
import gc
import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

asyncssh = pytest.importorskip("asyncssh")

from pathlib_next.uri.schemes.sftp import SftpPath  # noqa: E402
from pathlib_next.uri.schemes.sftp import _asyncssh as backend_mod  # noqa: E402

try:
    import paramiko
except ImportError:  # asyncssh-only install
    paramiko = None

#: Seconds to wait after a call has raised before the server's request counter
#: is read again. A walk that kept running after the exception issues hundreds
#: of requests a second on loopback, so any survivor shows within this time.
_SETTLE = 0.5

#: The asyncssh backend's default bound on requests in flight. A request that
#: was already on the wire when a call raised still reaches the server (and
#: its counter) afterwards, so the counter may move by at most this much.
_IN_FLIGHT = 16

_COUNTED = (
    "stat lstat fstat open close read write remove rmdir mkdir rename "
    "posix_rename setstat lsetstat fsetstat readlink symlink link realpath"
).split()


class _Wire:
    """Every request the server received, and what to do about them."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = collections.Counter()
        #: name -> hook(server, *args): runs first; raising fails the request.
        self.before = {}
        #: name -> hook(result, *args) -> result: reshapes the answer.
        self.after = {}

    def record(self, name):
        with self._lock:
            self.calls[name] += 1

    def total(self):
        with self._lock:
            return sum(self.calls.values())


def _counting_server_class(wire):
    class _Server(asyncssh.SFTPServer):
        pass

    def instrument(name):
        base = getattr(asyncssh.SFTPServer, name)

        def method(self, *args):
            wire.record(name)
            before = wire.before.get(name)
            if before is not None:
                before(self, *args)
            result = base(self, *args)
            after = wire.after.get(name)
            return result if after is None else after(result, *args)

        method.__name__ = name
        return method

    for name in _COUNTED:
        setattr(_Server, name, instrument(name))

    base_scandir = asyncssh.SFTPServer.scandir

    async def scandir(self, path):
        wire.record("scandir")
        async for entry in base_scandir(self, path):
            yield entry

    _Server.scandir = scandir
    return _Server


class _Loopback:
    """An asyncssh SFTP server on 127.0.0.1, serving `root`, on its own loop."""

    def __init__(self, root, wire):
        self.root = root
        self.wire = wire
        self.host_key = asyncssh.generate_private_key("ssh-rsa")
        self.live = set()
        self.loop = (
            asyncio.SelectorEventLoop()
            if sys.platform == "win32"
            else asyncio.new_event_loop()
        )
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.port = None
        self._server = None

    def _call(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self):
        state = self
        server_class = _counting_server_class(self.wire)
        root = str(self.root)

        class _NoAuth(asyncssh.SSHServer):
            def connection_made(self, conn):
                self._conn = conn
                state.live.add(conn)

            def connection_lost(self, exc):
                state.live.discard(self._conn)

            def begin_auth(self, username):
                return False

        async def _listen():
            return await asyncssh.listen(
                "127.0.0.1",
                0,
                server_factory=_NoAuth,
                server_host_keys=[self.host_key],
                sftp_factory=lambda chan: server_class(chan, chroot=root),
                process_factory=None,
            )

        self.thread.start()
        self._server = self._call(_listen())
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

    def url(self, path=""):
        return f"sftp://x:x@127.0.0.1:{self.port}/{path}"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """An empty home: nothing of the developer's `~/.ssh` is read."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    return root


@pytest.fixture
def wire():
    return _Wire()


@pytest.fixture
def server(root, wire):
    loopback = _Loopback(root, wire).start()
    try:
        yield loopback
    finally:
        loopback.stop()


@pytest.fixture
def backends():
    opened = []
    yield opened
    for backend in opened:
        backend.close()


def _backend(kind, backends, **kwargs):
    if kind == "paramiko":
        if paramiko is None:
            pytest.skip("paramiko not installed")
        from pathlib_next.uri.schemes.sftp import SftpBackend

        backend = SftpBackend(
            {"allow_agent": False, "look_for_keys": False},
            paramiko.AutoAddPolicy(),
            known_hosts=None,
        )
    else:
        backend = backend_mod.AsyncsshSftpBackend({"known_hosts": None}, **kwargs)
    backends.append(backend)
    return backend


@pytest.fixture(params=["paramiko", "asyncssh"])
def kind(request):
    return request.param


def _path(server, backend, rel=""):
    return SftpPath(server.url(rel), backend=backend)


def _body(name):
    return (name.encode() + b"|") * 800


def _make_tree(base, dirs=8, subdirs=3, files=20):
    """`dirs` x `subdirs` x `files` files under `base`, each with content of
    its own; returns {relative posix name: bytes}."""
    content = {}
    for d in range(dirs):
        for s in range(subdirs):
            folder = base / f"d{d}" / f"s{s}"
            folder.mkdir(parents=True)
            for f in range(files):
                name = f"d{d}/s{s}/f{f:02}.bin"
                (base / name).write_bytes(_body(name))
                content[name] = _body(name)
    return content


def _files(base):
    if not base.exists():
        return {}
    return {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in base.rglob("*")
        if path.is_file()
    }


def _entries(base):
    return sum(1 for _ in base.rglob("*")) if base.exists() else 0


def _deny(match, nth=1, *, onward=False, error=asyncssh.SFTPPermissionDenied):
    """A `wire.before` hook failing the `nth` request whose path `match`es
    (and every later one with `onward`)."""
    state = {"seen": 0}

    def hook(server, path, *args):
        if match(path):
            state["seen"] += 1
            if state["seen"] == nth or (onward and state["seen"] > nth):
                raise error("denied by the test")

    return hook


class _Sent:
    """When each request left the asyncssh client."""

    def __init__(self, monkeypatch):
        self.times = []
        send_packet = asyncssh.sftp.SFTPHandler.send_packet

        def recording(handler, *args, **kwargs):
            self.times.append(time.perf_counter())
            return send_packet(handler, *args, **kwargs)

        # The client's handler only: the in-process server's replies go
        # through the same base method.
        monkeypatch.setattr(
            asyncssh.sftp.SFTPClientHandler, "send_packet", recording, raising=False
        )

    def after(self, moment):
        return sum(1 for sent in self.times if sent > moment)


@pytest.fixture
def sent(monkeypatch):
    return _Sent(monkeypatch)


def _assert_stopped(wire, sent, raised_at):
    """Nothing was sent to the server after `raised_at`, and the server's
    counter moves by no more than the requests that were already in flight."""
    at_raise = wire.total()
    time.sleep(_SETTLE)
    assert sent.after(raised_at) == 0
    assert wire.total() - at_raise <= _IN_FLIGHT


def _interrupt_first_unbounded_wait(monkeypatch, after):
    """Deliver `KeyboardInterrupt` to the first `Future.result()` made without
    a timeout (the wait of a tree operation) once `after` seconds passed: what
    Ctrl-C does to a thread blocked in it."""
    real = concurrent.futures.Future.result
    armed = [True]

    def result(self, timeout=None):
        if timeout is None and armed[0]:
            armed[0] = False
            try:
                return real(self, after)
            except concurrent.futures.TimeoutError:
                raise KeyboardInterrupt from None
        return real(self, timeout)

    monkeypatch.setattr(concurrent.futures.Future, "result", result)


# --- a call that has raised has stopped --------------------------------------


def test_copy_that_raised_sends_no_further_request(server, root, wire, backends, sent):
    expected = _make_tree(root / "src")
    (root / "dst").mkdir()
    (root / "dst" / "untouched.txt").write_text("kept")
    backend = _backend("asyncssh", backends)
    src = _path(server, backend, "src")
    src.exists()
    wire.before["open"] = _deny(lambda p: b"/src/" in p, nth=40)

    with pytest.raises(PermissionError):
        src.copy(_path(server, backend, "dst"), recursive=True, overwrite=True)
    raised_at = time.perf_counter()

    _assert_stopped(wire, sent, raised_at)
    # Nothing the copy created is left half done, and nothing else is touched.
    copied = _files(root / "dst")
    assert (root / "dst" / "untouched.txt").read_text() == "kept"
    copied.pop("untouched.txt")
    assert copied
    assert len(copied) < len(expected)
    for name, content in copied.items():
        assert content == expected[name], name


def test_rm_that_raised_sends_no_further_request(server, root, wire, backends, sent):
    _make_tree(root / "tree")
    backend = _backend("asyncssh", backends)
    tree = _path(server, backend, "tree")
    tree.exists()
    denied = _deny(lambda p: p.endswith(b".bin"), nth=20)

    def slow_remove(server_, path, *args):
        time.sleep(0.002)
        denied(server_, path, *args)

    wire.before["remove"] = slow_remove

    with pytest.raises(PermissionError):
        tree.rm(recursive=True)
    raised_at = time.perf_counter()

    left = _entries(root / "tree")
    _assert_stopped(wire, sent, raised_at)
    assert left - _entries(root / "tree") <= _IN_FLIGHT


def test_rm_with_a_handler_that_declines_stops_at_the_first_error(
    server, root, wire, backends, sent
):
    _make_tree(root / "tree")
    backend = _backend("asyncssh", backends)
    tree = _path(server, backend, "tree")
    tree.exists()
    denied = _deny(lambda p: p.endswith(b".bin"), nth=20)

    def slow_remove(server_, path, *args):
        time.sleep(0.002)
        denied(server_, path, *args)

    wire.before["remove"] = slow_remove

    with pytest.raises(PermissionError):
        tree.rm(recursive=True, ignore_error=lambda error, path: False)
    raised_at = time.perf_counter()

    left = _entries(root / "tree")
    _assert_stopped(wire, sent, raised_at)
    assert left - _entries(root / "tree") <= _IN_FLIGHT


def test_walk_that_timed_out_has_stopped(server, root, wire, backends, sent):
    _make_tree(root / "tree")
    total = _entries(root / "tree")
    backend = _backend("asyncssh", backends)
    tree = _path(server, backend, "tree")
    aclient = tree._sftpclient._aclient

    def slow_remove(server_, path, *args):
        time.sleep(0.01)

    wire.before["remove"] = slow_remove

    with pytest.raises(TimeoutError):
        backend_mod._run(
            backend_mod._concurrent_rm(
                tree,
                max_concurrency=16,
                missing_ok=False,
                on_error=None,
                aclient=aclient,
            ),
            2.0,
        )
    raised_at = time.perf_counter()

    left = _entries(root / "tree")
    assert 0 < left < total
    _assert_stopped(wire, sent, raised_at)
    assert left - _entries(root / "tree") <= _IN_FLIGHT


def test_copy_that_timed_out_has_stopped_and_left_no_partial_file(
    server, root, wire, backends, sent
):
    expected = _make_tree(root / "src")
    (root / "dst").mkdir()
    backend = _backend("asyncssh", backends)
    src = _path(server, backend, "src")
    aclient = src._sftpclient._aclient

    def slow_write(server_, *args):
        time.sleep(0.01)

    wire.before["write"] = slow_write

    with pytest.raises(TimeoutError):
        backend_mod._run(
            backend_mod._concurrent_copy(
                src,
                _path(server, backend, "dst"),
                overwrite=False,
                follow_symlinks=True,
                preserve_metadata=False,
                max_concurrency=16,
                ignore_error=None,
                aclient=aclient,
            ),
            2.0,
        )
    raised_at = time.perf_counter()

    _assert_stopped(wire, sent, raised_at)
    copied = _files(root / "dst")
    assert 0 < len(copied) < len(expected)
    for name, content in copied.items():
        assert content == expected[name], name


@pytest.mark.parametrize("operation", ["copy", "rm"])
def test_walk_interrupted_in_the_calling_thread_has_stopped(
    operation, server, root, wire, backends, monkeypatch, sent
):
    expected = _make_tree(root / "src")
    backend = _backend("asyncssh", backends)
    src = _path(server, backend, "src")
    src.exists()

    def slow(server_, *args):
        time.sleep(0.01)

    wire.before["write" if operation == "copy" else "remove"] = slow
    _interrupt_first_unbounded_wait(monkeypatch, after=0.5)

    with pytest.raises(KeyboardInterrupt):
        if operation == "copy":
            src.copy(_path(server, backend, "dst"), recursive=True)
        else:
            src.rm(recursive=True)
    raised_at = time.perf_counter()

    _assert_stopped(wire, sent, raised_at)
    if operation == "copy":
        copied = _files(root / "dst")
        assert len(copied) < len(expected)
        for name, content in copied.items():
            assert content == expected[name], name
    else:
        assert _entries(root / "src") > 0


def test_failures_behind_the_raised_one_are_not_left_unretrieved(
    server, root, wire, backends, caplog
):
    _make_tree(root / "src")
    backend = _backend("asyncssh", backends)
    src = _path(server, backend, "src")
    src.exists()
    wire.before["open"] = _deny(lambda p: b"/src/" in p, nth=30, onward=True)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        with pytest.raises(PermissionError):
            src.copy(_path(server, backend, "dst"), recursive=True)
        time.sleep(0.3)
        gc.collect()

    assert [
        r.getMessage() for r in caplog.records if "never retrieved" in r.getMessage()
    ] == []


# --- the wait of _run() ---------------------------------------------------------


def test_run_waits_for_a_cancelled_coroutine_to_unwind_after_an_interrupt(
    monkeypatch,
):
    unwound = threading.Event()

    async def stalled():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)  # the cleanup requests of a real walk
            unwound.set()
            raise

    _interrupt_first_unbounded_wait(monkeypatch, after=0.1)

    with pytest.raises(KeyboardInterrupt):
        backend_mod._run(stalled(), None)
    assert unwound.is_set()


def test_run_waits_for_a_cancelled_coroutine_to_unwind_after_a_timeout():
    unwound = threading.Event()

    async def stalled():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)
            unwound.set()
            raise

    with pytest.raises(TimeoutError):
        backend_mod._run(stalled(), 0.1)
    assert unwound.is_set()


def test_run_returns_what_the_coroutine_returns_and_raises_what_it_raises():
    async def answer():
        return 42

    async def refuse():
        raise PermissionError("no")

    assert backend_mod._run(answer(), 5) == 42
    with pytest.raises(PermissionError):
        backend_mod._run(refuse(), 5)


# --- cancellation inside the walk --------------------------------------------------


class _Handle:
    def __init__(self, client, path, mode):
        self.client, self.path, self.mode = client, path, mode
        self.sent = False

    async def read(self, size=-1):
        if self.sent:
            return b""
        self.sent = True
        return b"data"

    async def write(self, data):
        pass

    async def close(self):
        self.client.closed.append((self.path, self.mode))


class _SlowOpenClient:
    """A client whose open of `/dst/a.txt` is on the wire until released."""

    def __init__(self):
        self.release = None
        self.requested = None
        self.closed = []
        self.removed = []
        self.chmods = []

    async def stat(self, path):
        if path == "/src":
            return asyncssh.SFTPAttrs(permissions=0o40755)
        if path == "/src/a.txt":
            return asyncssh.SFTPAttrs(permissions=0o100644)
        raise asyncssh.SFTPNoSuchFile(path)

    lstat = stat

    async def readdir(self, path):
        return [SimpleNamespace(filename="a.txt")]

    async def open(self, path, mode, encoding=None):
        if path == "/dst/a.txt":
            self.requested.set()
            await self.release.wait()
        return _Handle(self, path, mode)

    async def remove(self, path):
        self.removed.append(path)

    async def chmod(self, path, mode):
        self.chmods.append((path, mode))


class _FakePath:
    def __init__(self, path):
        self.path = path
        self.name = path.rsplit("/", 1)[1]

    def __truediv__(self, name):
        return _FakePath(f"{self.path}/{name}")


def test_cancelled_open_of_a_destination_is_completed_then_undone():
    async def scenario():
        client = _SlowOpenClient()
        client.release = asyncio.Event()
        client.requested = asyncio.Event()
        walk = asyncio.ensure_future(
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
        await client.requested.wait()
        walk.cancel()
        asyncio.get_running_loop().call_later(0.1, client.release.set)
        with pytest.raises(asyncio.CancelledError):
            await walk
        return client

    client = asyncio.run(scenario())
    # The request was already on the wire: it finished, its handle was closed
    # and the file it created was removed.
    assert ("/dst/a.txt", "wb") in client.closed
    assert client.removed == ["/dst/a.txt"]


def test_cancelled_walk_waits_for_a_running_error_handler():
    handled = threading.Event()
    started = threading.Event()

    def handler(error):
        started.set()
        time.sleep(0.3)
        handled.set()

    class _FailingClient(_SlowOpenClient):
        async def open(self, path, mode, encoding=None):
            if path == "/dst/a.txt":
                raise asyncssh.SFTPPermissionDenied("no")
            return _Handle(self, path, mode)

    async def scenario():
        walk = asyncio.ensure_future(
            backend_mod._concurrent_copy(
                _FakePath("/src"),
                _FakePath("/dst"),
                overwrite=False,
                follow_symlinks=True,
                preserve_metadata=False,
                max_concurrency=4,
                ignore_error=handler,
                aclient=_FailingClient(),
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        walk.cancel()
        with pytest.raises(asyncio.CancelledError):
            await walk
        # Checked before asyncio.run() joins its worker threads: the walk
        # itself has to have waited for the handler.
        assert handled.is_set()

    asyncio.run(scenario())
