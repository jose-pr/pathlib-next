"""What reaches a connection, and a ProxyCommand's argv, from an sftp: URI and
an ssh_config, on both backends.

Nothing here connects and nothing is spawned: `asyncssh.connect` and
`paramiko.ProxyCommand` are replaced by recorders, every config is a file under
`tmp_path` that the test passes explicitly, and HOME is an empty directory so
the developer's own `~/.ssh` is never read.
"""

import ipaddress
import pathlib

import pytest

from pathlib_next.uri import Source
from pathlib_next.uri.schemes import sftp as sftp_pkg
from pathlib_next.uri.schemes.sftp import SftpPath
from pathlib_next.uri.schemes.sftp import _sshconfig
from pathlib_next.uri.schemes.sftp._sshconfig import _DEFAULT_SSH_CONFIG

try:
    import asyncssh

    from pathlib_next.uri.schemes.sftp import _asyncssh as backend_mod
except ImportError:
    asyncssh = backend_mod = None

try:
    import paramiko

    from pathlib_next.uri.schemes.sftp import _paramiko as paramiko_mod
except ImportError:
    paramiko = paramiko_mod = None

needs_asyncssh = pytest.mark.skipif(asyncssh is None, reason="asyncssh not installed")
needs_paramiko = pytest.mark.skipif(paramiko is None, reason="paramiko not installed")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """An empty home: the default ssh_config would be this one's."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert pathlib.Path.home() == home
    assert _sshconfig._normalize_config_paths(_DEFAULT_SSH_CONFIG) == (
        str(home / ".ssh" / "config"),
    )
    return home


def _write(path, text):
    path.write_bytes(text.encode())
    return str(path)


class _Stopped(Exception):
    """Raised by the recorder that stands for `asyncssh.connect`."""


@pytest.fixture
def connects(monkeypatch):
    """The calls of `asyncssh.connect`, which never connects."""
    calls = []

    async def connect(*args, **kwargs):
        calls.append((args, kwargs))
        raise _Stopped

    monkeypatch.setattr(asyncssh, "connect", connect)
    return calls


@pytest.fixture
def proxy_commands(monkeypatch):
    """The command lines given to `paramiko.ProxyCommand`, which starts none."""
    commands = []

    class _Recorded:
        def __init__(self, command_line):
            commands.append(command_line)

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "ProxyCommand", _Recorded)
    return commands


# --- the port ----------------------------------------------------------------


@needs_asyncssh
def test_asyncssh_leaves_the_port_to_the_ssh_config_when_the_uri_names_none(
    tmp_path, connects
):
    config = _write(
        tmp_path / "config", "Host box\n  HostName 127.0.0.1\n  Port 4242\n"
    )
    backend = backend_mod.AsyncsshSftpBackend(ssh_config=config)

    with pytest.raises(_Stopped):
        backend.client(Source("sftp", "x:x", "box", None))

    ((args, kwargs),) = connects
    assert args == ("box",)
    assert "port" not in kwargs
    # What asyncssh then connects to, from the arguments it was given.
    options = asyncssh.SSHClientConnectionOptions(
        host=args[0], config=kwargs["config"], known_hosts=None
    )
    assert (options.host, options.port) == ("127.0.0.1", 4242)


@needs_asyncssh
def test_asyncssh_connects_to_the_port_a_uri_names_over_the_ssh_config(
    tmp_path, connects
):
    config = _write(
        tmp_path / "config", "Host box\n  HostName 127.0.0.1\n  Port 4242\n"
    )
    backend = backend_mod.AsyncsshSftpBackend(ssh_config=config)

    with pytest.raises(_Stopped):
        backend.client(Source("sftp", "x:x", "box", 2222))

    ((args, kwargs),) = connects
    assert args == ("box",)
    assert kwargs["port"] == 2222


# --- hosts that are not plain host names ------------------------------------------

_ACCEPTED = [
    "example.com",
    "host",
    "host_name",
    "_dmarc.example",
    "a-b.c_d",
    "host.example.",
    "Example.COM",
    "1.2.3.4",
    "xn--bcher-kva.example",
    "bücher.example",
    "日本語.example",
    "é.example",
    "::1",
    "2001:db8::1",
    "fe80::1%eth0",
    "fe80::1%12",
    ipaddress.IPv4Address("10.0.0.1"),
    ipaddress.IPv6Address("2001:db8::2"),
    ipaddress.IPv6Address("fe80::1%eth0"),
    None,
    "",
]

_REFUSED = [
    "host --flag",
    "-oProxyCommand=calc",
    "-",
    "--host",
    'ho"st',
    "ho'st",
    "ho`st",
    "ho$st",
    "ho;st",
    "ho|st",
    "ho&st",
    "ho<st",
    "ho>st",
    "ho(st)",
    "ho{st}",
    "ho\\st",
    "ho/st",
    "ho*st",
    "ho?st",
    "ho~st",
    "ho%st",
    "ho st",
    "ho\tst",
    "ho\nst",
    "ho\r\nst",
    "ho\x00st",
    "ho\x1fst",
    "ho\x7fst",
    "ho st",
    "ho st",
    "ho‮st",
    "bücher --flag",
    "host:22",
    "[::1]",
    "::1;calc",
    "fe80::1%",
    "fe80::1%a b",
    "1:2",
]


@pytest.mark.parametrize("host", _ACCEPTED, ids=repr)
def test_a_plain_host_name_or_address_is_accepted(host):
    _sshconfig._check_host(host)


@pytest.mark.parametrize("host", _REFUSED, ids=repr)
def test_a_host_that_is_not_a_plain_name_or_address_is_refused_naming_it(host):
    with pytest.raises(ValueError) as refused:
        _sshconfig._check_host(host)
    assert repr(host) in str(refused.value)


_HOSTILE_URIS = [
    "sftp://alice@host%20--evil-flag/x",
    "sftp://alice@-oProxyCommand=calc/x",
    "sftp://alice@host%22%20%22second/x",
    "sftp://alice@ho%00st/x",
    "sftp://alice@ho%0Ast/x",
    "sftp://alice@ho%3Bcalc/x",
]


@pytest.mark.parametrize("uri", _HOSTILE_URIS)
def test_pure_path_operations_on_a_refused_host_still_work(uri):
    path = SftpPath(uri)

    assert path.name == "x"
    assert (path / "y").name == "y"
    assert path.with_name("z").name == "z"
    assert path.parent.name == ""
    assert str(path).startswith("sftp://alice@")
    assert path == SftpPath(uri)


class _CountingBackend(sftp_pkg.BaseSftpBackend):
    built = []
    clients = []

    def __init__(self, ssh_config):
        type(self).built.append(ssh_config)

    @classmethod
    def default(cls, ssh_config=_DEFAULT_SSH_CONFIG):
        return cls(ssh_config)

    def client(self, source):
        type(self).clients.append(source)
        raise AssertionError("a connection was asked for")


class _CountingSftpPath(SftpPath):
    _default_backend_cls = _CountingBackend
    __SCHEMES = ()


@pytest.fixture
def counting():
    _CountingBackend.built.clear()
    _CountingBackend.clients.clear()
    return _CountingBackend


@pytest.mark.parametrize("uri", _HOSTILE_URIS)
def test_a_refused_host_fails_before_a_backend_is_built(uri, counting):
    path = _CountingSftpPath(uri)

    for operation in (
        path.stat,
        lambda: list(path.iterdir()),
        path.read_bytes,
        path.rmdir,
    ):
        with pytest.raises(ValueError, match="plain host name"):
            operation()

    assert counting.built == []
    assert counting.clients == []


@needs_paramiko
@pytest.mark.parametrize("uri", _HOSTILE_URIS)
def test_paramiko_refuses_a_host_before_reading_a_config_or_starting_a_proxy(
    uri, tmp_path, monkeypatch, proxy_commands
):
    config = _write(tmp_path / "config", "Host *\n  ProxyCommand prog %h %p %r\n")
    looked_up = []
    real = paramiko_mod._lookup_ssh_config
    monkeypatch.setattr(
        paramiko_mod,
        "_lookup_ssh_config",
        lambda *args: looked_up.append(args) or real(*args),
    )
    backend = sftp_pkg.SftpBackend(ssh_config=config)
    source = SftpPath(uri).source

    with pytest.raises(ValueError, match="plain host name"):
        backend.opts(source)
    with pytest.raises(ValueError, match="plain host name"):
        backend._known_hosts_files(source)
    with pytest.raises(ValueError, match="plain host name"):
        backend.transport(source)
    with pytest.raises(ValueError, match="plain host name"):
        SftpPath(uri, backend=backend).stat()

    assert looked_up == []
    assert proxy_commands == []


@needs_asyncssh
@pytest.mark.parametrize("uri", _HOSTILE_URIS)
def test_asyncssh_refuses_a_host_before_it_connects(uri, tmp_path, connects):
    config = _write(tmp_path / "config", "Host *\n  ProxyCommand prog %h %p\n")
    backend = backend_mod.AsyncsshSftpBackend(ssh_config=config)
    source = SftpPath(uri).source

    with pytest.raises(ValueError, match="plain host name"):
        backend.client(source)
    with pytest.raises(ValueError, match="plain host name"):
        SftpPath(uri, backend=backend).stat()

    assert connects == []


# --- ProxyCommand tokens (paramiko) -------------------------------------------------


def _proxy_command(tmp_path, proxy_commands, config_text, uri, **backend_args):
    config = _write(tmp_path / "config", config_text)
    backend = sftp_pkg.SftpBackend(ssh_config=config, **backend_args)
    backend.opts(SftpPath(uri).source)
    (command,) = proxy_commands
    return command


@needs_paramiko
def test_proxy_command_tokens_are_the_uris_host_port_and_user(tmp_path, proxy_commands):
    command = _proxy_command(
        tmp_path,
        proxy_commands,
        "Host *\n  ProxyCommand prog %h %p %r %n\n",
        "sftp://alice@example.invalid:2222/x",
    )

    assert command == "prog example.invalid 2222 alice example.invalid"


_ALIAS_CONFIG = (
    "Host box\n"
    "  HostName real.example\n"
    "  Port 2200\n"
    "  User cfg\n"
    "  ProxyCommand prog %h %p %r %n\n"
)


@needs_paramiko
def test_proxy_command_tokens_are_the_ssh_configs_where_the_uri_names_none(
    tmp_path, proxy_commands
):
    command = _proxy_command(tmp_path, proxy_commands, _ALIAS_CONFIG, "sftp://box/x")

    assert command == "prog real.example 2200 cfg box"


@needs_paramiko
def test_proxy_command_tokens_follow_the_uri_over_the_ssh_config(
    tmp_path, proxy_commands
):
    command = _proxy_command(
        tmp_path, proxy_commands, _ALIAS_CONFIG, "sftp://alice@box:2222/x"
    )

    assert command == "prog real.example 2222 alice box"


@needs_paramiko
def test_proxy_command_percent_percent_is_a_percent_and_values_are_not_rescanned(
    tmp_path, proxy_commands
):
    command = _proxy_command(
        tmp_path,
        proxy_commands,
        "Host *\n  ProxyCommand prog %%h %% 100%% %r %p\n",
        "sftp://al%25pice@host/x",
    )

    assert command == "prog %h % 100% al%pice 22"


@needs_paramiko
def test_proxy_command_uses_the_local_user_when_none_is_chosen(
    tmp_path, proxy_commands, monkeypatch
):
    monkeypatch.setattr(_sshconfig._getpass, "getuser", lambda: "localuser")

    command = _proxy_command(
        tmp_path, proxy_commands, "Host *\n  ProxyCommand prog %r\n", "sftp://host/x"
    )

    assert command == "prog localuser"


@needs_paramiko
@pytest.mark.parametrize(
    "uri",
    [
        "sftp://al%20ice@host/x",
        "sftp://-oFoo@host/x",
        "sftp://al%22ice@host/x",
        "sftp://alice%3Bcalc@host/x",
        "sftp://alice%24x@host/x",
        "sftp://alice%0Ax@host/x",
    ],
)
def test_proxy_command_refuses_a_user_that_would_split_the_command_line(
    uri, tmp_path, proxy_commands
):
    config = _write(tmp_path / "config", "Host *\n  ProxyCommand prog %h %r\n")

    with pytest.raises(ValueError, match="ProxyCommand"):
        sftp_pkg.SftpBackend(ssh_config=config).opts(SftpPath(uri).source)

    assert proxy_commands == []


@needs_paramiko
def test_a_user_is_only_checked_where_the_command_holds_it(tmp_path, proxy_commands):
    command = _proxy_command(
        tmp_path,
        proxy_commands,
        "Host *\n  ProxyCommand prog %h %p\n",
        "sftp://al%20ice@host/x",
    )

    assert command == "prog host 22"


@needs_paramiko
def test_no_proxy_is_started_for_proxy_command_none(tmp_path, proxy_commands):
    config = _write(tmp_path / "config", "Host *\n  ProxyCommand none\n")

    opts = sftp_pkg.SftpBackend(ssh_config=config).opts(
        SftpPath("sftp://host/x").source
    )

    assert "sock" not in opts
    assert proxy_commands == []


# --- a conditional Include ---------------------------------------------------------


def _lookup(config, host):
    return paramiko_mod._lookup_ssh_config(host, config)


def _include_config(tmp_path, home, included, config):
    (home / ".ssh" / "included.conf").write_bytes(included.encode())
    return _write(tmp_path / "config", config)


@needs_paramiko
def test_blocks_of_a_file_included_inside_a_host_block_apply_to_that_host_only(
    tmp_path, home
):
    config = _include_config(
        tmp_path,
        home,
        "User inner-default\nHost *\n  User leaked-user\n  ProxyCommand leaked %h\n",
        "Host internal\n  Include included.conf\n  Port 2200\nHost *\n  Port 22\n",
    )

    unrelated = _lookup(config, "unrelated.example")
    assert "user" not in unrelated
    assert "proxycommand" not in unrelated
    assert unrelated["port"] == "22"

    internal = _lookup(config, "internal")
    # The included file's own lines and blocks apply where the enclosing one
    # does, and the including file's following lines keep their block.
    assert internal["user"] == "inner-default"
    assert internal["proxycommand"] == "leaked %h"
    assert internal["port"] == "2200"


@needs_paramiko
@needs_asyncssh
def test_a_conditional_include_resolves_as_asyncssh_resolves_it(tmp_path, home):
    config = _include_config(
        tmp_path,
        home,
        "User inner-default\nHost *\n  User leaked-user\n",
        "Host internal\n  Include included.conf\n  Port 2200\nHost *\n  Port 22\n",
    )

    ours = _lookup(config, "internal")
    theirs = asyncssh.SSHClientConnectionOptions(
        config=[config], host="internal", known_hosts=None
    )
    assert (ours["user"], int(ours["port"])) == (theirs.username, theirs.port)

    ours = _lookup(config, "unrelated.example")
    theirs = asyncssh.SSHClientConnectionOptions(
        config=[config], host="unrelated.example", known_hosts=None
    )
    assert "user" not in ours
    assert theirs.username != "leaked-user"
    assert int(ours["port"]) == theirs.port


@needs_paramiko
def test_an_included_block_needs_both_its_own_pattern_and_the_enclosing_one(
    tmp_path, home
):
    config = _include_config(
        tmp_path,
        home,
        "Host box1\n  User one\nHost other\n  User other\n",
        "Host box*\n  Include included.conf\n  Compression yes\n",
    )

    assert _lookup(config, "box1")["user"] == "one"
    assert _lookup(config, "box1")["compression"] == "yes"
    assert "user" not in _lookup(config, "box2")
    assert _lookup(config, "box2")["compression"] == "yes"
    # `other` is in the included file but outside `box*`.
    assert "user" not in _lookup(config, "other")
    assert "compression" not in _lookup(config, "other")


@needs_paramiko
def test_a_negated_enclosing_pattern_keeps_its_exception(tmp_path, home):
    config = _include_config(
        tmp_path,
        home,
        "Host *\n  User from-include\n",
        "Host * !skip\n  Include included.conf\n",
    )

    assert _lookup(config, "anything")["user"] == "from-include"
    assert "user" not in _lookup(config, "skip")


@needs_paramiko
def test_a_conditional_include_inside_a_match_block(tmp_path, home):
    config = _include_config(
        tmp_path,
        home,
        "Port 2200\nHost b*\n  User b-user\nHost c*\n  User c-user\n",
        "Match originalhost a*,b*\n  Include included.conf\n",
    )

    assert _lookup(config, "a1")["port"] == "2200"
    assert "user" not in _lookup(config, "a1")
    assert _lookup(config, "b1")["user"] == "b-user"
    # `c*` is in the included file but outside the enclosing Match.
    assert "port" not in _lookup(config, "c1")
    assert "user" not in _lookup(config, "c1")


@needs_paramiko
def test_includes_nested_in_a_conditional_include_stay_conditional(tmp_path, home):
    (home / ".ssh" / "deep.conf").write_bytes(b"Host *\n  User deep\n")
    config = _include_config(
        tmp_path,
        home,
        "Host inner\n  Include deep.conf\n",
        "Host outer\n  Include included.conf\n",
    )

    assert "user" not in _lookup(config, "inner")
    assert "user" not in _lookup(config, "outer")


@needs_paramiko
def test_includes_nested_under_both_conditions_apply_where_both_hold(tmp_path, home):
    (home / ".ssh" / "deep.conf").write_bytes(b"User deep\nHost x*\n  User deep-x\n")
    config = _include_config(
        tmp_path,
        home,
        "Host o*\n  Include deep.conf\n",
        "Host o*\n  Include included.conf\n",
    )

    assert _lookup(config, "outer")["user"] == "deep"
    assert "user" not in _lookup(config, "inner")
    # `x*` is in the innermost file but outside `o*`.
    assert "user" not in _lookup(config, "x1")


@needs_paramiko
def test_a_top_level_include_keeps_its_blocks(tmp_path, home):
    config = _include_config(
        tmp_path,
        home,
        "Host alias\n  HostName 10.9.8.7\nHost *\n  User everyone\n",
        "Include included.conf\nHost late\n  Port 1\n",
    )

    assert _lookup(config, "alias")["hostname"] == "10.9.8.7"
    assert _lookup(config, "anything")["user"] == "everyone"
    assert _lookup(config, "late")["port"] == "1"


@needs_paramiko
def test_an_included_match_all_applies_wherever_the_enclosing_block_does(
    tmp_path, home
):
    config = _include_config(
        tmp_path,
        home,
        "Match all\n  User from-match-all\n",
        "Host here\n  Include included.conf\n",
    )

    assert _lookup(config, "here")["user"] == "from-match-all"
    assert "user" not in _lookup(config, "elsewhere")
