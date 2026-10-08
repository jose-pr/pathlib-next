"""What keeps the test session on this machine and independent of it.

`install_network_guard` refuses every route off the loopback interface that
Python code can take, `install_process_guard` refuses every program but the
running interpreter, and `isolate_environment` removes the proxy, cloud
credential and home-directory settings a developer's shell carries. The
fixtures and hooks that apply them are in `conftest.py`; the tests of these
functions are in `test_hermetic.py`.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys

#: Variables that are not the session's own: anything ending in `_PROXY`, the
#: cloud-SDK families, and the single names below.
AMBIENT_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GCLOUD_", "CLOUDSDK_", "BOTO_")
AMBIENT_NAMES = (
    "NETRC",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "STORAGE_EMULATOR_HOST",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
)

#: Programs the standard library starts to describe the host (`platform`,
#: `ctypes.util.find_library`) and `cmd`, which the Windows junction tests
#: run `mklink` through. None of them talks to a network.
HOST_PROBES = frozenset(
    {
        "cmd",
        "ver",
        "uname",
        "file",
        "getconf",
        "lsb_release",
        "sw_vers",
        "ldconfig",
        "gcc",
        "cc",
        "ld",
        "objdump",
        "crle",
    }
)

#: What a proxy-aware client is told never to proxy. A non-empty setting also
#: keeps `urllib` from reading the Windows registry or the macOS system
#: configuration for a proxy.
LOOPBACK_NO_PROXY = "127.0.0.1,localhost,::1"


class NetworkAccessBlocked(RuntimeError):
    """A test tried to reach a non-loopback host. Not an OSError on purpose:
    code that treats a network failure as "offline" must not swallow it."""


class ProcessSpawnBlocked(RuntimeError):
    """A test tried to start a program other than the running interpreter."""


def is_loopback_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").split("%", 1)[0]
    if host.lower() in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _address_host(address):
    # AF_INET/AF_INET6 tuples carry the host first; AF_UNIX paths are local.
    if isinstance(address, tuple) and address:
        return address[0]
    return None


def install_network_guard(monkeypatch, blocked):
    """Replace, for the rest of the test, every call that would send a packet
    off the loopback interface or resolve a name: it raises
    `NetworkAccessBlocked` before the original is reached, and appends a
    line to `blocked` so the test fails at teardown even if the error was
    swallowed. Each function is read when this runs, so a second call wraps
    the first (a test of the guard puts a recorder underneath it).

    Not covered: a C extension that calls the operating system directly, and
    a child process, which `install_process_guard` limits to the interpreter.
    """

    def check(host, what):
        if not is_loopback_host(host):
            blocked.append(f"{what} {host!r}")
            raise NetworkAccessBlocked(
                f"{what} to non-loopback host {host!r}; "
                "mark the test @pytest.mark.allow_network if intended"
            )

    def wrap_method(owner, name, what, address_of):
        original = getattr(owner, name)

        def guarded(self, *args, **kwargs):
            check(_address_host(address_of(args, kwargs)), what)
            return original(self, *args, **kwargs)

        guarded.__name__ = name
        monkeypatch.setattr(owner, name, guarded)

    def wrap_function(owner, name, what, host_of):
        original = getattr(owner, name)

        def guarded(*args, **kwargs):
            check(host_of(args, kwargs), what)
            return original(*args, **kwargs)

        guarded.__name__ = name
        monkeypatch.setattr(owner, name, guarded)

    def first(args, kwargs):
        return args[0] if args else None

    def sockaddr(args, kwargs):
        return _address_host(args[0] if args else None)

    wrap_method(socket.socket, "connect", "connect", lambda a, k: first(a, k))
    wrap_method(socket.socket, "connect_ex", "connect", lambda a, k: first(a, k))
    wrap_method(
        socket.socket,
        "sendto",
        "sendto",
        lambda a, k: a[-1] if a else None,
    )
    if hasattr(socket.socket, "sendmsg"):  # absent on Windows
        wrap_method(
            socket.socket,
            "sendmsg",
            "sendto",
            lambda a, k: a[3] if len(a) > 3 else k.get("address"),
        )
    wrap_function(socket, "getaddrinfo", "getaddrinfo", first)
    wrap_function(socket, "gethostbyname", "gethostbyname", first)
    wrap_function(socket, "gethostbyname_ex", "gethostbyname", first)
    wrap_function(socket, "gethostbyaddr", "gethostbyaddr", first)
    wrap_function(socket, "getnameinfo", "getnameinfo", sockaddr)

    if sys.platform == "win32":
        # The default Windows loop connects and sends through overlapped I/O,
        # which never calls the socket methods above.
        try:
            import asyncio.windows_events as windows_events
        except ImportError:  # pragma: no cover - an unusual build
            return
        proactor = windows_events.IocpProactor
        wrap_method(
            proactor, "connect", "connect", lambda a, k: a[1] if len(a) > 1 else None
        )
        wrap_method(
            proactor,
            "sendto",
            "sendto",
            lambda a, k: a[3] if len(a) > 3 else k.get("addr"),
        )


def _program_of(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        text = os.fsdecode(args).strip()
        # A string is a command line only under shell=True; its first word
        # is what would run either way.
        if text.startswith('"'):
            return text[1:].split('"', 1)[0]
        return text.split(None, 1)[0] if text else ""
    args = list(args)
    return os.fsdecode(args[0]) if args else ""


def install_process_guard(monkeypatch, blocked, allowed=()):
    """Allow `subprocess.Popen` (and so `run`, `check_output`, paramiko's
    ProxyCommand) to start only the running interpreter, the `HOST_PROBES`
    and the programs in `allowed`. A refused program raises
    `ProcessSpawnBlocked` before it starts."""
    own = os.path.normcase(os.path.abspath(sys.executable))
    names = HOST_PROBES | {name.lower() for name in allowed}
    original = subprocess.Popen.__init__

    def guarded(self, args, *rest, **kwargs):
        program = _program_of(args)
        base = os.path.splitext(os.path.basename(program).lower())[0]
        if os.path.normcase(os.path.abspath(program)) != own and base not in names:
            blocked.append(f"spawn {program!r}")
            raise ProcessSpawnBlocked(
                f"starting {program!r}: a test may only run the interpreter; "
                "replace subprocess.Popen with a recorder"
            )
        return original(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)


def is_ambient_variable(name: str) -> bool:
    upper = name.upper()
    return (
        upper.endswith("_PROXY")
        or upper.startswith(AMBIENT_PREFIXES)
        or upper in AMBIENT_NAMES
    )


def isolate_environment(patch, home) -> None:
    """Make the process environment the session's own: no proxy, cloud
    credential, `.netrc`, ssh-agent or CA-bundle setting of the developer's,
    and a home directory (`home`, a path) that holds nothing, so `~/.aws`,
    `~/.ssh` and `~/.netrc` are not there to be read. `patch` is a
    `MonkeyPatch`, which restores everything when it is undone."""
    home = os.fspath(home)
    for name in list(os.environ):
        if is_ambient_variable(name):
            patch.delenv(name)
    patch.setenv("NO_PROXY", LOOPBACK_NO_PROXY)
    patch.setenv("no_proxy", LOOPBACK_NO_PROXY)
    patch.setenv("HOME", home)
    patch.setenv("USERPROFILE", home)
    patch.setenv("AWS_CONFIG_FILE", os.path.join(home, "aws", "config"))
    patch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", os.path.join(home, "aws", "credentials")
    )
    patch.setenv("AWS_EC2_METADATA_DISABLED", "true")


#: Set and cleared by pytest itself around every phase of a test.
_PYTEST_OWN = ("PYTEST_CURRENT_TEST",)


def environment_snapshot() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _PYTEST_OWN}


def changed_variables(before: dict, after: dict) -> dict:
    """`{name: (old, new)}` for every variable that differs; `None` stands
    for absent."""
    return {
        name: (before.get(name), after.get(name))
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    }


def restore_environment(snapshot: dict) -> None:
    for name in list(os.environ):
        if name not in snapshot and name not in _PYTEST_OWN:
            del os.environ[name]
    os.environ.update(snapshot)
