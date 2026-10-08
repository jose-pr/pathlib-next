"""What a failed SFTP request raises, and how a listing treats names that are
not UTF-8, on both backends against a loopback server.

Both backends must agree on the exception a server status or a lost
connection becomes, so every scenario runs once per backend. The server
counts what it receives and can fail or reshape any request; nothing here
leaves the machine, and the home directory is an empty one so no `~/.ssh` of
the developer's is read.
"""

import errno
import logging
import pathlib
import socket
import threading

import pytest

asyncssh = pytest.importorskip("asyncssh")

from pathlib_next.uri.schemes import sftp as sftp_pkg  # noqa: E402
from pathlib_next.uri.schemes.sftp import SftpPath  # noqa: E402
from pathlib_next.uri.schemes.sftp import _asyncssh as backend_mod  # noqa: E402

from sftp_loopback import Loopback, Wire, name_on_disk  # noqa: E402

try:
    import paramiko
except ImportError:  # asyncssh-only install
    paramiko = None


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert pathlib.Path.home() == home
    return home


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    return root


@pytest.fixture
def wire():
    return Wire()


@pytest.fixture
def servers():
    started = []

    def start(root, wire, **kwargs):
        server = Loopback(root, wire, **kwargs).start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


@pytest.fixture
def server(root, wire, servers):
    return servers(root, wire)


@pytest.fixture(params=["paramiko", "asyncssh"])
def kind(request):
    if request.param == "paramiko" and paramiko is None:
        pytest.skip("paramiko not installed")
    return request.param


@pytest.fixture
def backends():
    opened = []
    yield opened
    for backend in opened:
        backend.close()


def make_backend(kind, backends, *, known_hosts=None, **kwargs):
    """A backend that trusts `known_hosts` (a file, or None for any key),
    reads no ssh_config and uses no agent or key of the developer's."""
    if kind == "paramiko":
        backend = sftp_pkg.SftpBackend(
            {"allow_agent": False, "look_for_keys": False},
            paramiko.AutoAddPolicy() if known_hosts is None else None,
            ssh_config=None,
            known_hosts=known_hosts,
            **kwargs,
        )
    else:
        opts = {"known_hosts": None if known_hosts is None else str(known_hosts)}
        backend = backend_mod.AsyncsshSftpBackend(opts, ssh_config=None, **kwargs)
    backends.append(backend)
    return backend


@pytest.fixture
def backend(kind, backends):
    return make_backend(kind, backends)


def unchained(error):
    """Neither the cause nor the context of `error` is shown: the library's
    exception, whose text can carry credentials, is not reachable from it."""
    return error.__cause__ is None and (
        error.__suppress_context__ or error.__context__ is None
    )


def path_at(server, backend, rel=""):
    return SftpPath(server.url(rel), backend=backend)


# --- a server status becomes the same exception on both backends ---------------


def test_missing_path_errors_carry_the_remote_path(server, backend):
    root = path_at(server, backend)
    for call in (
        lambda: (root / "nope").stat(),
        lambda: (root / "nope").open("rb"),
        lambda: (root / "nope").unlink(),
        lambda: (root / "nope" / "child").mkdir(),
        lambda: (root / "nope").rmdir(),
    ):
        with pytest.raises(FileNotFoundError) as raised:
            call()
        assert raised.value.errno == errno.ENOENT
        assert raised.value.filename is not None
        assert raised.value.filename.endswith("/nope") or "/nope/" in str(
            raised.value.filename
        )


def test_rename_error_names_both_paths_without_a_winerror(server, backend):
    root = path_at(server, backend)
    with pytest.raises(FileNotFoundError) as raised:
        (root / "nope").rename(root / "elsewhere")
    assert raised.value.errno == errno.ENOENT
    assert raised.value.filename.endswith("/nope")
    assert raised.value.filename2.endswith("/elsewhere")
    assert "WinError" not in str(raised.value)


def test_unsupported_setstat_is_not_implemented_and_a_copy_still_completes(
    server, root, wire, backend
):
    (root / "src.txt").write_bytes(b"payload")

    def unsupported(server_, *args):
        raise asyncssh.SFTPOpUnsupported("setstat not supported here")

    wire.before["setstat"] = unsupported
    src = path_at(server, backend, "src.txt")

    with pytest.raises(NotImplementedError):
        src.chmod(0o600)
    src.copy(path_at(server, backend, "copy.txt"))

    assert (root / "copy.txt").read_bytes() == b"payload"
    assert wire.calls["setstat"] >= 2


def test_connection_lost_status_is_a_connection_reset(server, wire, backend):
    def lost(server_, *args):
        raise asyncssh.SFTPConnectionLost("gone")

    wire.before["stat"] = lost

    with pytest.raises(ConnectionResetError) as raised:
        path_at(server, backend, "x").stat()
    assert raised.value.errno == errno.ECONNRESET
    assert unchained(raised.value)


def test_a_translated_status_is_not_chained_to_the_library_exception(server, backend):
    with pytest.raises(FileNotFoundError) as raised:
        path_at(server, backend, "nope").stat()
    assert unchained(raised.value)


# --- the connection, the login and the host key ---------------------------------


def test_wrong_password_is_an_authentication_error(root, wire, servers, kind, backends):
    server = servers(root, wire, password="right")
    backend = make_backend(kind, backends)

    with pytest.raises(sftp_pkg.SftpAuthenticationError) as raised:
        SftpPath(
            server.url("a", user="alice", password="wrong"), backend=backend
        ).stat()

    error = raised.value
    assert isinstance(error, PermissionError)
    assert error.errno == errno.EACCES
    assert "wrong" not in str(error) and "right" not in str(error)
    assert unchained(error)
    assert server.logins and all(given == "wrong" for _user, given in server.logins)


def test_right_password_logs_in(root, wire, servers, kind, backends):
    server = servers(root, wire, password="right")
    (root / "a").write_bytes(b"x")
    backend = make_backend(kind, backends)

    assert (
        SftpPath(server.url("a", password="right"), backend=backend).stat().st_size == 1
    )


def test_a_changed_host_key_is_a_host_key_error_and_no_credential_is_sent(
    root, wire, servers, kind, backends, tmp_path
):
    server = servers(root, wire, password="right")
    other = asyncssh.generate_private_key("ssh-rsa")
    algorithm, key = other.export_public_key("openssh").decode().split()[:2]
    known = tmp_path / "known_hosts"
    known.write_text(f"[{server.host}]:{server.port} {algorithm} {key}\n")
    backend = make_backend(kind, backends, known_hosts=known)

    with pytest.raises(sftp_pkg.SftpHostKeyError) as raised:
        SftpPath(server.url("a", password="right"), backend=backend).stat()

    error = raised.value
    assert isinstance(error, ConnectionError)
    assert error.errno == errno.ECONNABORTED
    assert "right" not in str(error)
    assert unchained(error)
    assert server.logins == []


def test_an_unknown_host_key_is_a_host_key_error(
    root, wire, servers, kind, backends, tmp_path
):
    server = servers(root, wire)
    known = tmp_path / "known_hosts"
    known.write_text("")
    backend = make_backend(kind, backends, known_hosts=known)

    with pytest.raises(sftp_pkg.SftpHostKeyError):
        SftpPath(server.url("a"), backend=backend).stat()


@pytest.fixture
def old_protocol_port():
    """The port of a loopback server that greets with SSH protocol version 1.5
    and then waits: both clients refuse it on their own, so the handshake
    fails for a reason that is neither the login nor the host key."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    accepted, done = [], threading.Event()

    def serve():
        while not done.is_set():
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            accepted.append(connection)
            try:
                connection.sendall(b"SSH-1.5-old\r\n")
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    done.set()
    thread.join(10)
    listener.close()
    for connection in accepted:
        connection.close()


_LIBRARY_ERROR = {"paramiko": "IncompatiblePeer", "asyncssh": "ProtocolNotSupported"}


def test_a_failed_handshake_logs_the_library_error_at_debug(
    old_protocol_port, kind, backends, caplog
):
    backend = make_backend(kind, backends)
    path = SftpPath(f"sftp://x:x@127.0.0.1:{old_protocol_port}/a", backend=backend)

    with caplog.at_level(logging.DEBUG, logger="pathlib_next.sftp"):
        with pytest.raises(ConnectionAbortedError) as raised:
            path.stat()

    name = _LIBRARY_ERROR[kind]
    error = raised.value
    assert error.errno == errno.ECONNABORTED
    assert str(error) == f"[Errno {errno.ECONNABORTED}] SFTP connection failed ({name})"
    assert unchained(error)
    records = [r for r in caplog.records if r.name == "pathlib_next.sftp"]
    assert [r.levelno for r in records] == [logging.DEBUG]
    message = records[0].getMessage()
    prefix = f"SFTP connection failed: {name}: "
    assert message.startswith(prefix) and len(message) > len(prefix)


def test_a_failed_handshake_logs_nothing_above_debug(
    old_protocol_port, kind, backends, caplog
):
    backend = make_backend(kind, backends)
    path = SftpPath(f"sftp://x:x@127.0.0.1:{old_protocol_port}/a", backend=backend)

    with caplog.at_level(logging.INFO, logger="pathlib_next.sftp"):
        with pytest.raises(ConnectionAbortedError):
            path.stat()

    assert [r for r in caplog.records if r.name == "pathlib_next.sftp"] == []


def test_a_trusted_host_key_connects(root, wire, servers, kind, backends, tmp_path):
    server = servers(root, wire)
    (root / "a").write_bytes(b"xy")
    known = tmp_path / "known_hosts"
    known.write_text(server.known_hosts_line())
    backend = make_backend(kind, backends, known_hosts=known)

    assert SftpPath(server.url("a"), backend=backend).stat().st_size == 2


def test_a_connection_closed_in_the_middle_of_a_request_is_a_connection_reset(
    server, root, wire, backend
):
    (root / "a").write_bytes(b"xy")
    path = path_at(server, backend, "a")
    assert path.stat().st_size == 2

    def drop(server_, *args):
        server.drop_connections()

    wire.before["stat"] = drop
    with pytest.raises(ConnectionResetError) as raised:
        path.stat()
    assert raised.value.errno == errno.ECONNRESET
    assert unchained(raised.value)

    # The next call reconnects.
    del wire.before["stat"]
    assert path.stat().st_size == 2
    assert server.connections == 2


def test_a_connection_closed_in_the_middle_of_a_read_is_a_connection_reset(
    server, root, wire, backend
):
    (root / "big").write_bytes(b"x" * 300_000)

    def drop(server_, *args):
        server.drop_connections()

    wire.before["read"] = drop
    with pytest.raises(ConnectionResetError):
        path_at(server, backend, "big").read_bytes()


def test_a_refused_connection_stays_a_connection_refused_error(backend, tmp_path):
    # Nothing listens here: a port the server released.
    server = Loopback(tmp_path).start()
    port = server.port
    server.stop()
    with pytest.raises(ConnectionError):
        SftpPath(f"sftp://x:x@127.0.0.1:{port}/a", backend=backend).stat()


# --- a transport failure does not trigger a second probe -------------------------


def test_a_lost_connection_in_mkdir_is_not_probed_again(server, wire, backend):
    path = path_at(server, backend, "d")
    path.parent.stat()

    def drop(server_, *args):
        server.drop_connections()

    wire.before["mkdir"] = drop
    wire.reset()
    with pytest.raises(ConnectionResetError):
        path.mkdir()
    assert wire.calls["stat"] == 0


# --- names that are not UTF-8 -----------------------------------------------------

ODD = "bad\udcff\udcfeutf8"


def odd_tree(root):
    tree = root / "tree"
    tree.mkdir()
    (tree / "good.txt").write_bytes(b"good")
    (tree / name_on_disk(ODD.encode("utf-8", "surrogateescape")).decode()).write_bytes(
        b"odd"
    )
    sub = tree / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_bytes(b"inner")
    return tree


@pytest.fixture
def odd_server(root, wire, servers):
    odd_tree(root)
    return servers(root, wire, escaped_names=True)


def test_a_non_utf8_name_lists_beside_its_siblings(odd_server, backend):
    tree = path_at(odd_server, backend, "tree")

    assert sorted(child.name for child in tree.iterdir()) == sorted(
        ["good.txt", ODD, "sub"]
    )
    walked = {name for _dir, _dirs, files in tree.walk() for name in files}
    assert walked == {"good.txt", ODD, "inner.txt"}
    assert sorted(match.name for match in tree.glob("*")) == sorted(
        ["good.txt", ODD, "sub"]
    )


def test_a_non_utf8_name_reads_and_stats(odd_server, backend):
    odd = path_at(odd_server, backend, "tree") / ODD

    assert odd.read_bytes() == b"odd"
    assert odd.stat().st_size == 3
    assert odd.is_file()


def test_a_non_utf8_name_is_removed_by_unlink_and_by_rm(odd_server, root, backend):
    tree = path_at(odd_server, backend, "tree")
    (tree / ODD).unlink()
    assert not (tree / ODD).exists()

    odd_tree_again = (
        root / "tree" / name_on_disk(ODD.encode("utf-8", "surrogateescape")).decode()
    )
    odd_tree_again.write_bytes(b"odd")
    tree.rm(recursive=True)

    assert not (root / "tree").exists()


def test_a_tree_with_a_non_utf8_name_copies_whole(odd_server, root, backend):
    tree = path_at(odd_server, backend, "tree")

    tree.copy(path_at(odd_server, backend, "copy"), recursive=True)

    copied = sorted(
        path.relative_to(root / "copy").as_posix()
        for path in (root / "copy").rglob("*")
        if path.is_file()
    )
    assert copied == sorted(
        [
            "good.txt",
            name_on_disk(ODD.encode("utf-8", "surrogateescape")).decode(),
            "sub/inner.txt",
        ]
    )
