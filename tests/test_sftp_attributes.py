"""Attributes, file objects, native checksums and the exit of an SFTP session,
on both backends: what the server is asked for, and what the caller gets back.

The loopback server counts the requests it receives and records the
attributes of a `setstat`, so every assertion is about what went over the
wire. Nothing here leaves the machine; the home directory is an empty one.
"""

import copy
import os
import pathlib
import pickle
import subprocess
import sys
import threading
import time

import pytest

asyncssh = pytest.importorskip("asyncssh")

from pathlib_next.uri import Source  # noqa: E402
from pathlib_next.uri.schemes import sftp as sftp_pkg  # noqa: E402
from pathlib_next.uri.schemes.sftp import SftpPath  # noqa: E402
from pathlib_next.uri.schemes.sftp import _asyncssh as backend_mod  # noqa: E402
from pathlib_next.uri.schemes.sftp import _checkfile  # noqa: E402
from pathlib_next.uri.schemes.sftp._sshconfig import _DEFAULT_SSH_CONFIG  # noqa: E402

from sftp_loopback import Loopback, Wire, counting_server_class  # noqa: E402

try:
    import paramiko
except ImportError:  # asyncssh-only install
    paramiko = None

needs_paramiko = pytest.mark.skipif(paramiko is None, reason="paramiko not installed")

SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


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


def make_backend(kind, backends, **kwargs):
    if kind == "paramiko":
        backend = sftp_pkg.SftpBackend(
            {"allow_agent": False, "look_for_keys": False},
            paramiko.AutoAddPolicy(),
            ssh_config=None,
            known_hosts=None,
            **kwargs,
        )
    else:
        backend = backend_mod.AsyncsshSftpBackend(
            {"known_hosts": None}, ssh_config=None, **kwargs
        )
    backends.append(backend)
    return backend


# --- owners: an unknown id is never sent as 0 ----------------------------------


class _SetstatRecorder:
    """A server class that records the attributes of every `setstat` and
    leaves the file alone."""

    def __init__(self, wire):
        self.seen = []
        recorder = self
        base = counting_server_class(wire)

        class _Server(base):
            def setstat(self, path, attrs):
                wire.record("setstat", (path,))
                recorder.seen.append(
                    (attrs.uid, attrs.gid, attrs.owner, attrs.group, attrs.permissions)
                )

        self.server_class = _Server


def _reports_owner(owner, group):
    """A `stat` answer that names the owner and the group, as a v4 server
    does, and carries no numeric ids."""

    def after(result, path):
        return asyncssh.SFTPAttrs(
            type=asyncssh.FILEXFER_TYPE_REGULAR,
            permissions=result.st_mode,
            size=result.st_size,
            mtime=int(result.st_mtime),
            atime=int(result.st_atime),
            owner=owner,
            group=group,
        )

    return after


@pytest.fixture
def v4(root, wire, servers, backends):
    recorder = _SetstatRecorder(wire)
    server = servers(root, wire, sftp_version=4, server_class=recorder.server_class)
    (root / "a.txt").write_bytes(b"hello")
    backend = make_backend("asyncssh", backends, sftp_version=4)
    path = SftpPath(server.url("a.txt"), backend=backend)
    return path, recorder


def test_a_partial_chown_against_a_server_that_names_owners_is_refused(v4, wire):
    path, recorder = v4
    wire.after["stat"] = _reports_owner("alice", "staff")

    with pytest.raises(NotImplementedError, match="uid"):
        path.chown(None, 4242)
    with pytest.raises(NotImplementedError, match="gid"):
        path.chown(4242, None)

    assert recorder.seen == []


def test_a_full_chown_against_a_server_that_names_owners_is_sent(v4, wire):
    path, recorder = v4
    wire.after["stat"] = _reports_owner("alice", "staff")

    path.chown(7, 8)

    assert [(owner, group) for _u, _g, owner, group, _p in recorder.seen] == [
        ("7", "8")
    ]


def test_a_numeric_owner_name_is_the_id(v4, wire):
    path, recorder = v4
    wire.after["stat"] = _reports_owner("1000", "1001")

    stat = path.stat()
    path.chown(None, 4242)

    assert (stat.st_uid, stat.st_gid) == (1000, 1001)
    assert [(owner, group) for _u, _g, owner, group, _p in recorder.seen] == [
        ("1000", "4242")
    ]


def test_a_partial_chown_against_a_v3_server_keeps_the_known_owner(
    root, wire, servers, kind, backends
):
    recorder = _SetstatRecorder(wire)
    server = servers(root, wire, sftp_version=3, server_class=recorder.server_class)
    (root / "a.txt").write_bytes(b"hello")
    path = SftpPath(server.url("a.txt"), backend=make_backend(kind, backends))
    stat = path.stat()

    path.chown(None, 4242)

    assert [(uid, gid) for uid, gid, _o, _g, _p in recorder.seen] == [
        (stat.st_uid, 4242)
    ]


# --- file objects ----------------------------------------------------------------


@pytest.fixture
def served(root, wire, servers, kind, backends):
    server = servers(root, wire)
    backend = make_backend(kind, backends)
    return SftpPath(server.url(), backend=backend), root


def test_write_bytes_returns_the_number_of_bytes(served):
    path, root = served

    assert (path / "w.bin").write_bytes(b"12345") == 5
    assert (path / "t.txt").write_text("héllo") == 5
    assert (root / "w.bin").read_bytes() == b"12345"


def test_write_of_an_exclusive_file_returns_the_count(served):
    path, root = served

    with (path / "x.bin").open("xb") as handle:
        assert handle.write(b"hi") == 2
        assert handle.write(bytearray(b"abc")) == 3
    assert (root / "x.bin").read_bytes() == b"hiabc"


def test_a_file_without_a_descriptor_says_so(served):
    path, _root = served
    with (path / "f.bin").open("wb") as handle:
        with pytest.raises(OSError):
            handle.fileno()


def test_truncate_cuts_the_remote_file(served):
    path, root = served
    (root / "t.bin").write_bytes(b"0123456789")

    with (path / "t.bin").open("r+b") as handle:
        assert handle.truncate(4) == 4
    assert (root / "t.bin").read_bytes() == b"0123"

    with (path / "t.bin").open("r+b") as handle:
        handle.seek(2)
        assert handle.truncate() == 2
    assert (root / "t.bin").read_bytes() == b"01"


def test_truncate_after_a_buffered_write_cuts_what_was_written(served):
    path, root = served

    with (path / "t.bin").open("w+b") as handle:
        handle.write(b"abcdefgh")
        handle.truncate(3)
    assert (root / "t.bin").read_bytes() == b"abc"


# --- the default sentinels survive a copy and a pickle ---------------------------


def test_the_default_ssh_config_is_still_itself_after_a_pickle_or_a_copy():
    for convert in (
        copy.copy,
        copy.deepcopy,
        lambda value: pickle.loads(pickle.dumps(value)),
    ):
        assert convert(_DEFAULT_SSH_CONFIG) is _DEFAULT_SSH_CONFIG


@needs_paramiko
def test_a_copied_or_pickled_paramiko_backend_still_reads_the_default_config():
    from pathlib_next.uri.schemes.sftp import _paramiko

    backend = sftp_pkg.SftpBackend()
    source = Source("sftp", None, "somehost", None)
    for clone in (
        copy.deepcopy(backend),
        pickle.loads(pickle.dumps(backend)),
    ):
        assert clone.ssh_config is _DEFAULT_SSH_CONFIG
        assert clone.known_hosts is _paramiko._DEFAULT_KNOWN_HOSTS
        assert clone.opts(source)["hostname"] == "somehost"
        assert clone._known_hosts_files(source) == []


class _PickledPath(SftpPath):
    __SCHEMES = ()


if paramiko is not None:

    class _QuietParamiko(sftp_pkg.SftpBackend):
        __slots__ = ()

        @classmethod
        def default(cls, ssh_config=_DEFAULT_SSH_CONFIG):
            return cls(
                {"allow_agent": False, "look_for_keys": False},
                None,
                ssh_config=ssh_config,
            )


def test_a_pickled_or_copied_path_stats_against_the_server(
    root, wire, servers, kind, home, monkeypatch
):
    server = servers(root, wire)
    (root / "a.txt").write_bytes(b"hello")
    (home / ".ssh" / "known_hosts").write_text(server.known_hosts_line())
    monkeypatch.setattr(
        _PickledPath,
        "_default_backend_cls",
        _QuietParamiko if kind == "paramiko" else backend_mod.AsyncsshSftpBackend,
    )
    path = _PickledPath(server.url("a.txt"))
    try:
        for clone in (
            pickle.loads(pickle.dumps(path)),
            pickle.loads(pickle.dumps(_PickledPath(server.url("a.txt")))),
            copy.deepcopy(path),
        ):
            assert clone.stat().st_size == 5
            assert clone.read_bytes() == b"hello"
    finally:
        path.backend.close()


# --- native checksums --------------------------------------------------------------

#: The bytes of the digest of each algorithm the check-file draft names.
_SIZES = {
    "md5": 16,
    "sha1": 20,
    "sha224": 28,
    "sha256": 32,
    "sha384": 48,
    "sha512": 64,
    "crc32": 4,
}
_DIGESTS = {name: bytes(range(size)) for name, size in _SIZES.items()}


def _reply(algorithm, digest=None):
    import struct

    def string(text):
        data = text.encode()
        return struct.pack(">I", len(data)) + data

    return string(algorithm) + (_DIGESTS[algorithm] if digest is None else digest)


class _Answers:
    """A paramiko-shaped client whose extension request is answered by
    `answer(algorithm, call_number)`: a payload, or an exception to raise."""

    class _Handle:
        handle = b"h"

        def close(self):
            pass

    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def open(self, path, mode, buffering=-1):
        return self._Handle()

    def _request(self, command, extension, handle, algorithm, *rest):
        import paramiko.sftp as paramiko_sftp

        self.requests.append(algorithm)
        outcome = self.answer(algorithm, len(self.requests))
        if isinstance(outcome, BaseException):
            raise outcome
        return paramiko_sftp.CMD_EXTENDED_REPLY, paramiko_message(outcome)


def paramiko_message(payload):
    from paramiko.message import Message

    message = Message(payload)
    return message


@pytest.fixture
def checksum_path(monkeypatch):
    """`path.checksum()` over a fake paramiko client: the real backend code,
    the real reply parser, a client that answers as the test says."""
    pytest.importorskip("paramiko")
    from pathlib_next.uri.schemes.sftp._paramiko import SftpBackend

    monkeypatch.setattr(_checkfile, "_SUPPORT_CACHE", {})

    def build(answer):
        client = _Answers(answer)
        backend = SftpBackend.__new__(SftpBackend)
        monkeypatch.setattr(SftpBackend, "client", lambda self, source: client)
        return SftpPath("sftp://host/a.txt", backend=backend), backend, client

    return build


def test_a_refused_algorithm_does_not_switch_off_the_others(checksum_path):
    def answer(algorithm, _call):
        if algorithm == "md5":
            return NotImplementedError("md5 is disabled")
        return _reply(algorithm)

    path, _backend, client = checksum_path(answer)

    with pytest.raises(NotImplementedError):
        path.checksum("md5")
    assert path.checksum("sha256") == _DIGESTS["sha256"].hex()
    with pytest.raises(NotImplementedError):
        path.checksum("md5")

    # md5 was asked for once, sha256 once: the refusal is remembered per
    # algorithm and says nothing about the others.
    assert client.requests == ["md5", "sha256"]


def test_a_connection_that_produced_a_digest_is_never_reported_empty(checksum_path):
    def answer(algorithm, _call):
        if algorithm == "md5":
            return NotImplementedError("md5 is disabled")
        return _reply(algorithm)

    path, backend, client = checksum_path(answer)

    assert path.checksum("sha256") == _DIGESTS["sha256"].hex()
    supported = backend.supported_checksums(path)
    with pytest.raises(NotImplementedError):
        path.checksum("md5")

    assert "sha256" in supported
    assert "md5" not in backend.supported_checksums(path)
    assert backend.supported_checksums(path)
    assert client.requests == ["sha256", "md5"]


def test_a_failure_for_one_file_refuses_nothing(checksum_path):
    def answer(algorithm, call):
        if call == 1:
            return OSError("I/O error reading this one file")
        return _reply(algorithm)

    path, _backend, client = checksum_path(answer)

    with pytest.raises(NotImplementedError):
        path.checksum("md5")
    assert path.checksum("md5") == _DIGESTS["md5"].hex()
    assert client.requests == ["md5", "md5"]


def test_a_refusal_of_the_whole_extension_costs_one_request_per_algorithm(
    checksum_path,
):
    path, backend, client = checksum_path(
        lambda algorithm, _call: NotImplementedError("operation unsupported")
    )

    for _ in range(3):
        with pytest.raises(NotImplementedError):
            path.checksum("md5")
    assert backend.supported_checksums(path) == frozenset()
    assert client.requests == ["md5"]


@pytest.mark.parametrize("algorithm", ["MD5", "sha-256", "md5,sha1", "", "blake2"])
def test_an_algorithm_the_draft_does_not_name_is_not_sent(checksum_path, algorithm):
    path, _backend, client = checksum_path(lambda name, _call: _reply("md5"))

    with pytest.raises(NotImplementedError):
        path.checksum(algorithm)
    assert client.requests == []


@pytest.mark.parametrize("algorithm", sorted(_SIZES))
def test_a_reply_is_a_digest_only_of_the_size_the_algorithm_has(algorithm):
    size = _SIZES[algorithm]
    assert _checkfile.parse_reply(_reply(algorithm), algorithm) == (
        _DIGESTS[algorithm].hex()
    )
    for wrong in (b"", bytes(size - 1), bytes(size + 1), bytes(2 * size)):
        with pytest.raises(NotImplementedError):
            _checkfile.parse_reply(_reply(algorithm, wrong), algorithm)


def test_a_reply_for_an_algorithm_without_a_known_size_is_refused():
    for algorithm in ("crc64", "MD5", "sha-256"):
        with pytest.raises(NotImplementedError):
            _checkfile.parse_reply(_reply("md5", b"\x00" * 3), algorithm)


# --- unclosed files are written when the interpreter exits ----------------------

_CHILD = """
import sys
from pathlib_next.uri.schemes.sftp import SftpPath

url, kind, mode = sys.argv[1:4]
if kind == "paramiko":
    import paramiko
    from pathlib_next.uri.schemes.sftp import SftpBackend

    backend = SftpBackend(
        {"allow_agent": False, "look_for_keys": False},
        paramiko.AutoAddPolicy(),
        ssh_config=None,
        known_hosts=None,
    )
else:
    from pathlib_next.uri.schemes.sftp import AsyncsshSftpBackend

    backend = AsyncsshSftpBackend({"known_hosts": None}, ssh_config=None)
path = SftpPath(url, backend=backend)
handle = path.open("wb" if mode == "bytes" else "w")
handle.write(b"buffered, never closed" if mode == "bytes" else "text, never closed")
"""


@pytest.mark.parametrize("mode", ["bytes", "text"])
def test_a_file_left_open_at_exit_is_written(root, wire, servers, kind, mode):
    server = servers(root, wire)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [SRC, *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )

    finished = subprocess.run(
        [sys.executable, "-c", _CHILD, server.url("left.txt"), kind, mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert finished.returncode == 0, finished.stderr
    assert "shutting down" not in finished.stderr
    expected = b"buffered, never closed" if mode == "bytes" else b"text, never closed"
    assert (root / "left.txt").read_bytes() == expected


def test_the_exit_hook_does_not_wait_for_a_connection_that_hangs(monkeypatch):
    release = threading.Event()

    class _Hung:
        def close(self):
            release.wait(30)

    hung = _Hung()
    sftp_pkg._UNCLOSED.add(hung)
    monkeypatch.setattr(sftp_pkg, "_EXIT_GRACE", 0.3)
    try:
        started = time.monotonic()
        sftp_pkg._close_unclosed()
        assert time.monotonic() - started < 10
    finally:
        release.set()
        sftp_pkg._UNCLOSED.discard(hung)


# --- one default backend for the paths of one endpoint ---------------------------


def test_a_fresh_root_its_parent_and_twenty_children_open_one_connection(
    root, wire, servers, kind, home, monkeypatch
):
    server = servers(root, wire)
    for number in range(20):
        (root / f"c{number}.txt").write_bytes(b"x")
    (home / ".ssh" / "known_hosts").write_text(server.known_hosts_line())
    monkeypatch.setattr(
        SftpPath,
        "_default_backend_cls",
        _QuietParamiko if kind == "paramiko" else backend_mod.AsyncsshSftpBackend,
    )

    path = SftpPath(server.url("c0.txt"))
    try:
        assert path.parent.exists()
        assert path.exists()
        assert all((path.parent / f"c{n}.txt").exists() for n in range(20))
    finally:
        path.backend.close()

    assert server.connections == 1
