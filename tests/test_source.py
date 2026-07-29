from pathlib_next.uri.source import Source


def test_bool_false_when_all_empty():
    assert not Source(None, None, None, None)
    assert not Source("", "", "", None)


def test_bool_true_when_any_set():
    assert Source("http", None, None, None)
    assert Source(None, None, "host", None)
    assert Source(None, None, None,80)


def test_parsed_userinfo_splits_user_password():
    s = Source(None, "user:pass", None, None)
    assert s.parsed_userinfo() == ("user", "pass")


def test_parsed_userinfo_no_password():
    s = Source(None, "user", None, None)
    assert s.parsed_userinfo() == ("user", "")


def test_parsed_userinfo_empty():
    s = Source(None, None, None, None)
    assert s.parsed_userinfo() == ("", "")


def test_from_str():
    s = Source.from_str("http://user:pass@host:80")
    assert s.scheme == "http"
    assert s.host == "host"
    assert s.port == 80


def test_is_local_localhost():
    # B29: is_local() is lru_cache'd; localhost short-circuits before any
    # DNS lookup, so this is safe/fast regardless.
    assert Source(None, None, "localhost", None).is_local()


def test_is_local_no_host():
    assert Source(None, None, None, None).is_local()
    assert Source(None, None, "", None).is_local()


def test_is_local_cached_same_result():
    s = Source(None, None, "localhost", None)
    assert s.is_local() is s.is_local()


def test_is_local_loopback_ip():
    # is_local() now delegates the "is this address mine" check to
    # netimps.is_local_address(), which answers loopback without
    # requiring interface enumeration -- see netimps' own is_local_address
    # docstring.
    assert Source(None, None, "127.0.0.1", None).is_local()


def test_is_local_loopback_ipv6_literal():
    # Constructed via from_str() (not a raw Source(...) string field) so
    # _decode_host() parses the bracketed literal into a real IPv6Address
    # -- the isinstance(host, str) branch in is_local() is then skipped
    # entirely.
    s = Source.from_str("sftp://root@[::1]:22", strict=False)
    assert s.is_local()


def test_is_local_bare_ipv6_string_host():
    # B-fix: a directly-constructed Source with host as a bare IPv6 str
    # (bypassing _decode_host()'s usual bracket-literal parsing -- a
    # supported construction pattern, Source's fields are public) used to
    # crash with socket.gaierror when is_local() still used gethostbyname()
    # (IPv4-only). is_local() now tries netimps.try_parse() first, which
    # handles any IP literal (v4 or v6) directly, before falling through to
    # hostname resolution.
    assert Source(None, None, "::1", None).is_local()


def test_is_local_own_hostname():
    # Exercises the real hostname->address resolution path end to end
    # (netimps.resolve()'s default backend chain), not just IP literals.
    import socket

    assert Source(None, None, socket.gethostname(), None).is_local()


def test_is_local_false_for_nonresolving_hostname():
    # A hostname that genuinely doesn't resolve anywhere now correctly
    # returns False (definitive non-local answer) rather than raising --
    # netimps.resolve()'s contract is "always a list, empty on genuine
    # lookup failure, never an exception for a not-found name" (see
    # netimps.resolve()'s own docstring). This is an improvement over the
    # old socket.gethostbyname()-based implementation, which raised
    # socket.gaierror for the same input -- callers like
    # utils.sync._is_local() that used to catch that exception and treat
    # it as "local" (safe-default fallback) now get a real answer instead.
    assert not Source(
        None, None, "this-host-does-not-exist.invalid", None
    ).is_local()


def test_is_local_own_interface_address():
    # A real address assigned to this machine (not just loopback) must
    # also be local -- exercises netimps.get_interfaces() enumeration,
    # not just the loopback fast path. `.ips` yields IPv4Interface/
    # IPv6Interface (CIDR-aware) objects -- `.ip` is the bare address
    # Source.host/is_local() actually deals in.
    import netimps

    own_ips = [
        str(ip.ip)
        for iface in netimps.get_interfaces()
        for ip in iface.ips
        if not ip.ip.is_link_local  # avoid a bare-form zone-id ambiguity
    ]
    assert own_ips, "test host has no non-link-local interface address"
    assert Source(None, None, own_ips[0], None).is_local()


def test_is_local_false_for_public_address():
    assert not Source(None, None, "8.8.8.8", None).is_local()


def test_getitem_by_name_and_index():
    s = Source("http", "u", "h", 80)
    assert s["scheme"] == "http"
    assert s[0] == "http"
    assert s[2] == "h"


def test_str_uses_uricompose():
    s = Source("http", None, "host", 80)
    assert str(s) == "http://host:80"
