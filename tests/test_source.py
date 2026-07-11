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


def test_getitem_by_name_and_index():
    s = Source("http", "u", "h", 80)
    assert s["scheme"] == "http"
    assert s[0] == "http"
    assert s[2] == "h"


def test_str_uses_uricompose():
    s = Source("http", None, "host", 80)
    assert str(s) == "http://host:80"
