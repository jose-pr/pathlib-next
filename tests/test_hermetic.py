"""The session guards themselves: every route off the loopback interface is
refused before the operating system is asked, and the process environment is
the session's own.

A guard is tested by putting a recorder where the real call was and asserting
that the recorder is never reached for a remote address; nothing here opens a
connection to anything.
"""

import asyncio
import importlib.util
import os
import pathlib
import socket
import subprocess
import sys
import urllib.request

import pytest

import conftest
import hermetic
from hermetic import NetworkAccessBlocked, ProcessSpawnBlocked

REPO = pathlib.Path(__file__).resolve().parent.parent
REMOTE = ("203.0.113.9", 80)  # TEST-NET-3: documentation addresses only
REMOTE_V6 = ("2001:db8::9", 80, 0, 0)


class Recorder:
    """Stands in for a low-level call; remembers what reached it."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _guard_over(monkeypatch, owner, names, result=None):
    """Replace `owner.<name>` by recorders, then install the guard over them."""
    recorders = {}
    for name in names:
        recorders[name] = Recorder(result)
        monkeypatch.setattr(owner, name, recorders[name])
    blocked = []
    hermetic.install_network_guard(monkeypatch, blocked)
    return recorders, blocked


def test_a_connect_to_a_remote_host_is_refused_before_the_socket_is_touched(
    monkeypatch,
):
    recorders, blocked = _guard_over(monkeypatch, socket.socket, ["connect"])
    recorders["connect_ex"] = Recorder(0)
    monkeypatch.setattr(socket.socket, "connect_ex", recorders["connect_ex"])
    hermetic.install_network_guard(monkeypatch, blocked)

    with socket.socket() as sock:
        for call in (sock.connect, sock.connect_ex):
            for address in (REMOTE, REMOTE_V6, ("example.invalid", 80)):
                with pytest.raises(NetworkAccessBlocked, match="non-loopback"):
                    call(address)
    assert recorders["connect"].calls == []
    assert recorders["connect_ex"].calls == []
    assert len(blocked) == 6


def test_a_connect_to_loopback_or_a_local_path_reaches_the_socket(monkeypatch):
    recorders, blocked = _guard_over(monkeypatch, socket.socket, ["connect"])
    with socket.socket() as sock:
        sock.connect(("127.0.0.1", 9))
        sock.connect(("::1", 9, 0, 0))
        sock.connect(("localhost", 9))
        sock.connect("a-unix-socket-path")
    assert len(recorders["connect"].calls) == 4
    assert blocked == []


def test_a_datagram_to_a_remote_host_is_refused_before_it_is_sent(monkeypatch):
    names = ["sendto"] + (["sendmsg"] if hasattr(socket.socket, "sendmsg") else [])
    recorders, blocked = _guard_over(monkeypatch, socket.socket, names)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(NetworkAccessBlocked):
            sock.sendto(b"x", REMOTE)
        with pytest.raises(NetworkAccessBlocked):
            sock.sendto(b"x", 0, REMOTE)
        if "sendmsg" in recorders:
            with pytest.raises(NetworkAccessBlocked):
                sock.sendmsg([b"x"], [], 0, REMOTE)
        sock.sendto(b"x", ("127.0.0.1", 9))
    assert len(recorders["sendto"].calls) == 1
    assert "sendmsg" not in recorders or recorders["sendmsg"].calls == []
    assert all(line.startswith("sendto ") for line in blocked)


@pytest.mark.parametrize(
    "name, remote, local",
    [
        ("getaddrinfo", ("example.invalid", 80), ("127.0.0.1", 80)),
        ("gethostbyname", ("example.invalid",), ("localhost",)),
        ("gethostbyname_ex", ("example.invalid",), ("127.0.0.1",)),
        ("gethostbyaddr", ("203.0.113.9",), ("127.0.0.1",)),
        ("getnameinfo", (REMOTE, 0), (("127.0.0.1", 80), 0)),
    ],
)
def test_a_name_lookup_for_a_remote_host_is_refused_before_it_is_made(
    monkeypatch, name, remote, local
):
    recorders, blocked = _guard_over(monkeypatch, socket, [name])
    with pytest.raises(NetworkAccessBlocked):
        getattr(socket, name)(*remote)
    assert recorders[name].calls == []
    assert len(blocked) == 1
    getattr(socket, name)(*local)
    assert len(recorders[name].calls) == 1


@pytest.mark.skipif(
    sys.platform != "win32", reason="the proactor event loop is Windows-only"
)
def test_an_overlapped_connect_to_a_remote_host_is_refused(monkeypatch):
    # The Windows default event loop connects without touching the socket
    # methods, so its proactor is guarded as well.
    import asyncio.windows_events as windows_events

    recorders, blocked = _guard_over(
        monkeypatch, windows_events.IocpProactor, ["connect", "sendto"]
    )
    with socket.socket() as sock:
        with pytest.raises(NetworkAccessBlocked):
            windows_events.IocpProactor.connect(None, sock, REMOTE)
        with pytest.raises(NetworkAccessBlocked):
            windows_events.IocpProactor.sendto(None, sock, b"x", 0, REMOTE)
        windows_events.IocpProactor.connect(None, sock, ("127.0.0.1", 9))
    assert recorders["sendto"].calls == []
    assert len(recorders["connect"].calls) == 1
    assert len(blocked) == 2


def test_the_default_event_loop_cannot_be_used_to_reach_a_remote_host(
    monkeypatch, _block_non_loopback_network
):
    # Whatever loop this platform builds by default, a connection attempt to
    # a remote literal address ends in the guard, with the lowest call the
    # loop would make (the socket's, or the Windows proactor's) replaced by
    # a recorder. The loop is built first: on Windows its self-pipe connects
    # a socket pair.
    loop = asyncio.new_event_loop()
    try:
        lowest = [(socket.socket, "connect")]
        windows_events = sys.modules.get("asyncio.windows_events")
        if windows_events and isinstance(loop, windows_events.ProactorEventLoop):
            lowest.append((windows_events.IocpProactor, "connect"))
        recorders = []
        for owner, name in lowest:
            recorders.append(Recorder())
            monkeypatch.setattr(owner, name, recorders[-1])
        hermetic.install_network_guard(monkeypatch, _block_non_loopback_network)

        async def attempt():
            with socket.socket() as sock:
                sock.setblocking(False)
                await loop.sock_connect(sock, REMOTE)

        with pytest.raises(NetworkAccessBlocked):
            loop.run_until_complete(attempt())
    finally:
        loop.close()
    assert [recorder.calls for recorder in recorders] == [[]] * len(lowest)
    assert _block_non_loopback_network == [f"connect {REMOTE[0]!r}"]
    _block_non_loopback_network.clear()


def test_a_program_other_than_the_interpreter_is_refused_before_it_starts(monkeypatch):
    started = []

    def record(self, args, *rest, **kwargs):
        started.append(args)
        self._child_created = False  # what Popen.__del__ reads

    monkeypatch.setattr(subprocess.Popen, "__init__", record)
    blocked = []
    hermetic.install_process_guard(monkeypatch, blocked)

    for command in (
        ["ssh", "jump", "nc", "%h", "%p"],
        "ssh jump nc host 22",
        ["git", "status"],
        [os.path.join(os.path.dirname(sys.executable), "not-python.exe")],
    ):
        with pytest.raises(ProcessSpawnBlocked):
            subprocess.Popen(command, shell=isinstance(command, str))
    assert started == []
    assert len(blocked) == 4

    allowed = [
        [sys.executable, "-c", "pass"],
        ["cmd", "/c", "echo"],
        ["uname", "-p"],
        ["/sbin/ldconfig", "-p"],
    ]
    for command in allowed:
        subprocess.Popen(command)
    assert started == allowed


def test_a_marked_program_is_let_through_and_another_is_not(monkeypatch):
    started = []

    def record(self, args, *rest, **kwargs):
        started.append(args)
        self._child_created = False

    monkeypatch.setattr(subprocess.Popen, "__init__", record)
    blocked = []
    hermetic.install_process_guard(monkeypatch, blocked, allowed=["BasedPyright"])
    subprocess.Popen(["/usr/bin/basedpyright", "--version"])
    with pytest.raises(ProcessSpawnBlocked):
        subprocess.Popen(["pyright", "--version"])
    assert started == [["/usr/bin/basedpyright", "--version"]]


def test_the_isolated_environment_has_no_proxy_cloud_or_home_setting(
    tmp_path, monkeypatch
):
    with pytest.MonkeyPatch.context() as patch:
        # The developer's shell, as far as this test's own process is concerned.
        for name, value in {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "ALL_PROXY": "socks5://127.0.0.1:9",
            "AWS_PROFILE": "work",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
            "AZURE_STORAGE_CONNECTION_STRING": "x",
            "GOOGLE_APPLICATION_CREDENTIALS": "x",
            "NETRC": "x",
            "REQUESTS_CA_BUNDLE": "x",
            "SSH_AUTH_SOCK": "x",
            "PATHLIB_NEXT_KEEP": "yes",
        }.items():
            patch.setenv(name, value)
        hermetic.isolate_environment(patch, tmp_path)

        assert os.environ["PATHLIB_NEXT_KEEP"] == "yes"
        for gone in (
            "HTTP_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "AWS_PROFILE",
            "AWS_ENDPOINT_URL",
            "AZURE_STORAGE_CONNECTION_STRING",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "NETRC",
            "REQUESTS_CA_BUNDLE",
            "SSH_AUTH_SOCK",
        ):
            assert gone not in os.environ, gone
        assert os.path.expanduser("~") == str(tmp_path)
        assert pathlib.Path.home() == tmp_path
        assert os.environ["AWS_CONFIG_FILE"].startswith(str(tmp_path))
        assert os.environ["AWS_SHARED_CREDENTIALS_FILE"].startswith(str(tmp_path))
        # A non-empty environment answer keeps urllib (and so requests) from
        # consulting the Windows registry or the system settings for a proxy.
        assert urllib.request.getproxies() == {"no": hermetic.LOOPBACK_NO_PROXY}

    assert "HTTP_PROXY" not in os.environ or os.environ["HTTP_PROXY"] != (
        "http://127.0.0.1:9"
    )


def test_a_proxy_in_the_developers_shell_is_not_used_for_a_loopback_server(
    http_server, monkeypatch
):
    requests = pytest.importorskip("requests")
    assert requests.get(f"{http_server}/a.txt", timeout=10).text == "a"


# --- the session itself, in a child pytest ------------------------------------


def _probe(name, tmp_path, extra_env=None):
    home = tmp_path / "developer-home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "config").write_text("[default]\nregion = mars-1\n")
    (home / ".netrc").write_text("machine h login u password p\n")
    env = dict(os.environ)
    env.update(
        HTTP_PROXY="http://127.0.0.1:9",
        HTTPS_PROXY="http://127.0.0.1:9",
        https_proxy="http://127.0.0.1:9",
        ALL_PROXY="http://127.0.0.1:9",
        AWS_PROFILE="a-profile-that-does-not-exist",
        AWS_ENDPOINT_URL="http://127.0.0.1:9",
        HOME=str(home),
        USERPROFILE=str(home),
        PATHLIB_NEXT_DEVELOPER_HOME=str(home),
    )
    env.update(extra_env or {})
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO / "src"), env.get("PYTHONPATH")])
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(REPO / "tests" / "probes" / name),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_session_started_from_a_shell_with_proxies_and_aws_settings_is_isolated(
    tmp_path,
):
    result = _probe("probe_session_environment.py", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    # The third probe needs `requests`, which a bare install does not have.
    expected = "3 passed" if importlib.util.find_spec("requests") else "2 passed"
    assert expected in result.stdout, result.stdout


def test_a_test_that_leaves_the_environment_changed_fails_at_teardown(tmp_path):
    result = _probe("probe_environment_leak.py", tmp_path)
    assert result.returncode != 0
    assert "2 passed, 1 error" in result.stdout, result.stdout
    assert "left the process environment changed" in result.stdout
    assert "PATHLIB_NEXT_LEAKED: None -> 'yes'" in result.stdout


def test_the_header_names_the_modules_left_out_without_the_uri_extra(monkeypatch):
    monkeypatch.setattr(conftest, "collect_ignore", ["test_a.py", "test_b.py"])
    header = conftest.pytest_report_header(None)
    assert "2 test modules are not collected: test_a, test_b" in header
    monkeypatch.setattr(conftest, "collect_ignore", [])
    assert conftest.pytest_report_header(None) is None
