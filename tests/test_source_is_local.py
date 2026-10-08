"""`Source.is_local()` answers for the host alone and remembers the answer for
a bounded time. The resolver and the interface list are replaced by stubs, so
no name is looked up."""

from __future__ import annotations

import pytest

pytest.importorskip("uritools")
netimps = pytest.importorskip("netimps")

from pathlib_next.uri import source as source_module
from pathlib_next.uri.source import Source


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture(autouse=True)
def _fresh_cache():
    source_module._local_cache.clear()
    yield
    source_module._local_cache.clear()


@pytest.fixture
def resolver(monkeypatch):
    """Replaces the lookup: `resolver.answers[name]` is what `name` resolves
    to, `resolver.queries` records every name asked."""

    class Resolver:
        def __init__(self):
            self.answers = {}
            self.queries = []
            self.local = set()

    stub = Resolver()

    def resolve(host, rdtype, *args, **kwargs):
        stub.queries.append((host, rdtype))
        return list(stub.answers.get(host, [])) if rdtype == "a" else []

    monkeypatch.setattr(netimps, "resolve", resolve)
    monkeypatch.setattr(
        netimps, "is_local_address", lambda address: str(address) in stub.local
    )
    return stub


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(source_module, "_time", clock)
    return clock


def test_sources_that_differ_only_in_credentials_or_port_share_one_lookup(resolver):
    resolver.answers["files.invalid"] = ["192.0.2.7"]
    resolver.local.add("192.0.2.7")
    sources = [
        Source("sftp", f"user:pw{i}", "files.invalid", None) for i in range(3)
    ] + [
        Source("ftp", None, "files.invalid", 21),
        Source("http", "other", "files.invalid", 8080),
    ]
    assert all(source.is_local() for source in sources)
    assert resolver.queries == [("files.invalid", "a"), ("files.invalid", "aaaa")]


def test_the_cache_holds_hosts_not_sources_so_no_password_stays_in_it(resolver):
    Source("sftp", "user:S3CRET", "files.invalid", None).is_local()
    assert list(source_module._local_cache) == ["files.invalid"]
    assert "S3CRET" not in repr(source_module._local_cache)


def test_an_answer_is_not_kept_forever(resolver, clock):
    resolver.answers["files.invalid"] = ["192.0.2.7"]
    resolver.local.add("192.0.2.7")
    source = Source("sftp", None, "files.invalid", None)
    assert source.is_local() is True
    # The interface went away: the stale answer is still served, for a while.
    resolver.local.clear()
    clock.now += source_module._LOCAL_TTL - 1
    assert source.is_local() is True
    assert len(resolver.queries) == 2
    clock.now += 2
    assert source.is_local() is False
    assert len(resolver.queries) == 4
    # A fresh, equal Source gets the new answer too.
    assert Source("sftp", None, "files.invalid", None).is_local() is False


def test_the_cache_is_bounded(resolver, monkeypatch):
    monkeypatch.setattr(netimps, "is_local_address", lambda address: False)
    limit = source_module._LOCAL_CACHE_SIZE
    for index in range(limit + 40):
        Source(None, None, f"10.1.{index // 200}.{index % 200}", None).is_local()
    assert len(source_module._local_cache) <= limit


def test_expired_entries_leave_before_live_ones(resolver, clock, monkeypatch):
    monkeypatch.setattr(netimps, "is_local_address", lambda address: False)
    monkeypatch.setattr(source_module, "_LOCAL_CACHE_SIZE", 4)
    for index in range(4):
        Source(None, None, f"10.2.0.{index}", None).is_local()
    clock.now += source_module._LOCAL_TTL + 1
    Source(None, None, "10.2.0.9", None).is_local()
    assert list(source_module._local_cache) == ["10.2.0.9"]


def test_loopback_literals_and_empty_hosts_need_no_lookup(resolver):
    assert Source(None, None, "", None).is_local()
    assert Source(None, None, None, None).is_local()
    assert Source("http", "u:p", "localhost", 80).is_local()
    assert resolver.queries == []
    assert source_module._local_cache == {}
