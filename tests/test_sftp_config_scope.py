"""Which paths of one SFTP endpoint share a backend, and which `ssh_config`
each one reads.

A path carries its `ssh_config` to every path derived from it on the same
endpoint and drops it, with the backend, when the derived path is on another
host. Two paths of one endpoint whose `ssh_config` differs do not share the
backend they derive for themselves, whichever of them does I/O first. Nothing
here connects: the default backend is a recorder that remembers the
`ssh_config` it was built for.
"""

import pytest

from pathlib_next.uri import Source
from pathlib_next.uri.schemes import sftp as sftp_pkg
from pathlib_next.uri.schemes.sftp import SftpPath
from pathlib_next.uri.schemes.sftp._sshconfig import _DEFAULT_SSH_CONFIG


class _Recorded(sftp_pkg.BaseSftpBackend):
    built = []

    def __init__(self, ssh_config):
        self.ssh_config = ssh_config
        type(self).built.append(self)

    @classmethod
    def default(cls, ssh_config=_DEFAULT_SSH_CONFIG):
        return cls(ssh_config)


#: `with_source()` and a destination string dispatch on the registered class,
#: so the recorder is installed on `SftpPath` itself for each test.
_ConfigPath = SftpPath


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    _Recorded.built.clear()
    monkeypatch.setattr(SftpPath, "_default_backend_cls", _Recorded)


def path(url="sftp://h/a", **kwargs):
    return _ConfigPath(url, **kwargs)


# --- with_source and a join to another host --------------------------------------


def test_with_source_on_the_same_endpoint_keeps_the_ssh_config():
    base = path(ssh_config=None)

    moved = base.with_source(Source("sftp", None, "h", None))

    assert moved._ssh_config is None
    assert moved.backend.ssh_config is None
    assert moved.backend is base.backend


def test_with_source_on_another_host_takes_neither_the_config_nor_the_backend():
    base = path(ssh_config="/etc/special")
    base.backend

    moved = base.with_source(Source("sftp", None, "other", None))

    assert moved._ssh_config is _DEFAULT_SSH_CONFIG
    assert moved.backend is not base.backend
    assert moved.backend.ssh_config is _DEFAULT_SSH_CONFIG


def test_a_join_onto_another_host_does_not_carry_the_ssh_config():
    base = path("sftp://a/x", ssh_config="CONFIG-A")

    joined = _ConfigPath(base, "sftp://other/y")

    assert joined.source.host == "other"
    assert joined._ssh_config is _DEFAULT_SSH_CONFIG
    assert joined.backend.ssh_config is _DEFAULT_SSH_CONFIG


def test_a_join_on_the_same_endpoint_carries_the_ssh_config():
    base = path("sftp://a/x", ssh_config="CONFIG-A")

    for joined in (
        _ConfigPath(base, "y"),
        _ConfigPath(base, "sftp://a/y"),
        base / "y",
        base.parent,
        base.with_name("z"),
    ):
        assert joined._ssh_config == "CONFIG-A"
        assert joined.backend.ssh_config == "CONFIG-A"
    assert len(_Recorded.built) == 1


def test_a_str_destination_on_another_host_does_not_carry_the_ssh_config():
    base = path("sftp://a/x", ssh_config="CONFIG-A")

    same = base._coerce_target("sftp://a/y")
    other = base._coerce_target("sftp://b/y")

    assert same._ssh_config == "CONFIG-A"
    assert other._ssh_config is _DEFAULT_SSH_CONFIG


# --- relatives with different configurations -------------------------------------


@pytest.mark.parametrize("first", ["a", "b"])
def test_relatives_with_different_configs_do_not_share_a_derived_backend(first):
    a = path("sftp://h/a", ssh_config="CONFIG-A")
    b = _ConfigPath(a, "b", ssh_config="CONFIG-B")

    for member in (a, b) if first == "a" else (b, a):
        member.backend

    assert a.backend.ssh_config == "CONFIG-A"
    assert b.backend.ssh_config == "CONFIG-B"
    assert a.backend is not b.backend
    # Each one's own derivations stay with it.
    for child in (a / "x", a.parent, a.with_name("y")):
        assert child.backend is a.backend
    for child in (b / "x", b.parent, b.with_name("y")):
        assert child.backend is b.backend
    assert len(_Recorded.built) == 2


def test_a_relative_with_its_own_config_does_not_take_a_backend_already_built():
    a = path("sftp://h/a", ssh_config="CONFIG-A")
    built_for_a = a.backend

    b = _ConfigPath(a, "b", ssh_config="CONFIG-B")

    assert b.backend is not built_for_a
    assert b.backend.ssh_config == "CONFIG-B"
    assert (a / "x").backend is built_for_a


def test_a_derivation_given_its_own_config_starts_its_own_family():
    a = path("sftp://h/a", ssh_config="CONFIG-A")
    a.backend

    b = a._from_parsed_parts(a.source, "/b", None, None, ssh_config="CONFIG-B")

    assert b.backend is not a.backend
    assert b.backend.ssh_config == "CONFIG-B"
    assert (b / "x").backend is b.backend


def test_a_relative_given_the_same_config_shares_the_backend():
    a = path("sftp://h/a", ssh_config="CONFIG-A")

    same = _ConfigPath(a, "b", ssh_config="CONFIG-A")
    default_a = path("sftp://h/a")
    default_b = _ConfigPath(default_a, "b", ssh_config=_DEFAULT_SSH_CONFIG)

    assert same.backend is a.backend
    assert default_b.backend is default_a.backend


def test_a_supplied_backend_still_wins_over_the_config_rule():
    supplied = _Recorded("SUPPLIED")
    a = _ConfigPath("sftp://h/a", ssh_config="CONFIG-A", backend=supplied)

    assert (a / "x").backend is supplied
    assert _ConfigPath(a, "b", ssh_config="CONFIG-B").backend is supplied


def test_one_default_backend_serves_a_root_its_parent_and_twenty_children():
    root = path("sftp://h/dir/file.txt")

    root.parent.backend
    root.backend
    children = [root.parent / f"c{number}" for number in range(20)]
    for child in children:
        child.backend

    assert len(_Recorded.built) == 1
    assert all(child.backend is root.backend for child in children)
