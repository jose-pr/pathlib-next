"""The paramiko backend's caches: connections of threads that ended, and the
parsed ssh_config of a file that changed."""

import pathlib
import threading

import pytest

paramiko = pytest.importorskip("paramiko")
pytest.importorskip("asyncssh")

from pathlib_next.uri.schemes import sftp as sftp_pkg  # noqa: E402
from pathlib_next.uri.schemes.sftp import SftpPath  # noqa: E402
from pathlib_next.uri.schemes.sftp import _paramiko  # noqa: E402

from sftp_loopback import Loopback, Wire  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert pathlib.Path.home() == home
    return home


def test_a_connection_of_a_thread_that_ended_is_closed_by_the_next_new_one(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")
    server = Loopback(root, Wire()).start()
    backend = sftp_pkg.SftpBackend(
        {"allow_agent": False, "look_for_keys": False},
        paramiko.AutoAddPolicy(),
        ssh_config=None,
        known_hosts=None,
    )
    try:
        source = SftpPath(server.url("a.txt")).source
        clients = []
        worker = threading.Thread(target=lambda: clients.append(backend.client(source)))
        worker.start()
        worker.join()
        (stale,) = clients
        assert stale.sock.get_transport().is_active()

        # A new connection (this thread had none) sweeps up the dead thread's.
        fresh = backend.client(source)

        assert not stale.sock.get_transport().is_active()
        assert [
            key[2] for key in _paramiko._CACHED_CLIENTS.cache if key[0] is backend
        ] == [threading.get_ident()]
        assert backend.client(source) is fresh
    finally:
        backend.close()
        server.stop()


def test_a_changed_ssh_config_file_is_read_again(tmp_path):
    config = tmp_path / "config"
    config.write_text("Host box\n  HostName 10.0.0.1\n")
    assert _paramiko._lookup_ssh_config("box", str(config))["hostname"] == "10.0.0.1"

    config.write_text("Host box\n  HostName 10.0.0.2\n  Port 2200\n")

    found = _paramiko._lookup_ssh_config("box", str(config))
    assert (found["hostname"], found["port"]) == ("10.0.0.2", "2200")


def test_an_unchanged_ssh_config_file_is_parsed_once(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text("Host box\n  HostName 10.0.0.1\n")
    parsed = []
    real = _paramiko._SSHConfig.parse

    def counting(self, file_obj):
        parsed.append(1)
        return real(self, file_obj)

    monkeypatch.setattr(_paramiko._SSHConfig, "parse", counting)

    for _ in range(3):
        _paramiko._lookup_ssh_config("box", str(config))

    assert len(parsed) == 1


def test_a_missing_ssh_config_file_is_no_config(tmp_path):
    assert _paramiko._lookup_ssh_config("box", str(tmp_path / "absent")) == {}
