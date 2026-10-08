"""How an authority (userinfo, host, port) is read and written: the parse
(`Uri._parse_uri`, `Source.from_str`) and the composer (`Uri.as_uri`,
`Source.as_str`) agree with each other and with RFC 3986 / RFC 6874."""

from __future__ import annotations

import ipaddress

import pytest

from pathlib_next.uri import Uri, UriPath, _same_authority
from pathlib_next.uri.source import Source

# --- a host of digits ------------------------------------------------------


@pytest.mark.parametrize(
    "url, host",
    [
        ("s3://20240101/key", "20240101"),
        ("gs://123456/k", "123456"),
        ("az://1234567/c/k", "1234567"),
        ("http://12345/x", "12345"),
        ("sftp://u@007/x", "007"),
        ("http://12345:80/x", "12345"),
    ],
)
def test_a_host_of_digits_is_the_host(url, host):
    path = UriPath(url)
    assert path.source.host == host
    assert path.as_uri() == url
    assert Uri(url).source.host == host


def test_a_digit_host_names_the_bucket_of_an_object_store_path():
    path = UriPath("s3://20240101/key/part")
    assert path.parent.as_uri() == "s3://20240101/key"
    assert (path / "x").source.host == "20240101"


# --- userinfo ----------------------------------------------------------------


def test_an_escaped_colon_in_the_user_name_is_not_the_separator():
    uri = Uri("sftp://us%3Aer:S3CRET@h/p")
    assert uri.source.parsed_userinfo() == ("us:er", "S3CRET")
    assert uri.as_uri() == "sftp://us%3Aer:S3CRET@h/p"
    assert str(uri) == "sftp://us%3Aer@h/p"


def test_an_escaped_colon_in_the_password_stays_in_the_password():
    uri = Uri("sftp://user:p%3Aw%40x@h/p")
    assert uri.source.parsed_userinfo() == ("user", "p:w@x")
    assert Uri(uri.as_uri()).source.parsed_userinfo() == ("user", "p:w@x")


def test_a_user_name_with_an_escaped_colon_and_no_password():
    uri = Uri("sftp://us%3Aer@h/p")
    assert uri.source.parsed_userinfo() == ("us:er", "")
    assert uri.as_uri() == "sftp://us%3Aer@h/p"


def test_userinfo_still_reads_as_the_decoded_text():
    userinfo = Uri("sftp://user:S3CRET@h/p").source.userinfo
    assert userinfo == "user:S3CRET"
    assert isinstance(userinfo, str)
    assert Source("sftp", "a:b", "h", None).parsed_userinfo() == ("a", "b")
    assert Source("sftp", "a", "h", None).parsed_userinfo() == ("a", "")
    assert Source("sftp", None, "h", None).parsed_userinfo() == ("", "")


def test_two_users_that_differ_only_in_the_split_are_two_endpoints():
    split_early = Uri("sftp://us:er%3AS3CRET@h/p").source
    split_late = Uri("sftp://us%3Aer:S3CRET@h/p").source
    assert split_early.userinfo == split_late.userinfo
    assert not _same_authority(split_early, split_late)
    assert _same_authority(split_late, Uri("sftp://us%3Aer:S3CRET@h/q").source)


# --- the port ----------------------------------------------------------------


def test_a_port_above_65535_is_refused():
    assert Uri("http://h:65535/").source.port == 65535
    with pytest.raises(ValueError, match="0-65535"):
        Uri("http://h:65536/").source


def test_a_huge_port_is_refused_the_same_way_on_every_interpreter():
    with pytest.raises(ValueError, match="0-65535"):
        Uri("http://h:" + "9" * 5000 + "/").source
    assert Uri("http://h:00080/x").source.port == 80


@pytest.mark.parametrize("tail", ["abc", "-1", "+80", "8 0", "80a"])
def test_a_colon_followed_by_anything_but_digits_is_an_invalid_port(tail):
    with pytest.raises(ValueError, match="port"):
        Uri(f"http://h:{tail}/").source


def test_an_empty_port_is_no_port():
    assert Uri("http://h:/x").source.port is None


def test_port_zero_is_kept_and_is_not_the_same_endpoint_as_no_port():
    uri = Uri("http://h:0/x")
    assert uri.source.port == 0
    assert uri.as_uri() == "http://h:0/x"
    assert not _same_authority(uri.source, Uri("http://h/x").source)


def test_a_port_is_not_echoed_with_the_password_in_the_message():
    with pytest.raises(ValueError) as caught:
        Uri("sftp://user:S3CRET@h:99999/p").source
    assert "S3CRET" not in str(caught.value)


@pytest.mark.parametrize(
    "source",
    [
        Source("http", None, "h", "80/evil"),
        Source("http", None, "h", -5),
        Source("http", None, "h", 65536),
        Source("http", None, "h", 1.5),
        Source("a b", None, "h", 80),
        Source("1x", None, "h", None),
        Source("http://evil", None, "h", None),
    ],
)
def test_a_source_that_was_not_parsed_cannot_compose_a_different_uri(source):
    with pytest.raises(ValueError):
        source.as_str()
    with pytest.raises(ValueError):
        Uri("/p").with_source(source).as_uri()


def test_a_port_given_as_digits_composes_as_a_number():
    assert Source("http", "u", "h", "80").as_str() == "http://u@h:80"


# --- an IPv6 zone ---------------------------------------------------------------


def test_the_zone_of_an_ipv6_literal_follows_the_escaped_percent():
    uri = Uri("http://[fe80::1%25eth0]:80/x")
    host = uri.source.host
    assert host == ipaddress.IPv6Address("fe80::1%eth0")
    assert host.scope_id == "eth0"
    assert uri.as_uri() == "http://[fe80::1%25eth0]:80/x"


def test_an_unescaped_percent_zone_is_read_and_written_escaped():
    uri = Uri("http://[fe80::1%eth0]/x")
    assert uri.source.host.scope_id == "eth0"
    assert uri.as_uri() == "http://[fe80::1%25eth0]/x"


def test_a_percent_encoded_zone_is_decoded_and_encoded_again():
    uri = Uri("http://[fe80::1%25en%30]/x")
    assert uri.source.host.scope_id == "en0"
    assert (
        Uri("/p")
        .with_source(Source("http", None, ipaddress.IPv6Address("fe80::1%a b"), None))
        .as_uri()
        == "http://[fe80::1%25a%20b]/p"
    )


def test_a_zone_survives_a_host_given_as_text():
    assert Source("http", None, "[fe80::1%25eth0]", None).as_str() == (
        "http://[fe80::1%25eth0]"
    )
    assert Source("http", None, "fe80::1%eth0", None).as_str() == (
        "http://[fe80::1%25eth0]"
    )


# --- one composer ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://b\u00fccher.example",
        "http://u:p@h:80",
        "http://[::1]:80",
        "file:",
        "file:///",
        "x:",
        "//h",
        "",
        "http://h",
        "sftp://us%3Aer:p%40w@h",
    ],
)
def test_source_as_str_is_what_as_uri_writes_for_that_source(url):
    uri = Uri(url)
    for sanitize in (True, False):
        assert uri.source.as_str(sanitize=sanitize) == uri.with_path("").as_uri(
            sanitize=sanitize
        )


def test_source_str_renders_a_non_ascii_host_as_idna():
    assert str(Uri("http://b\u00fccher.example").source) == (
        "http://xn--bcher-kva.example"
    )


# --- messages and reprs never carry the secret -------------------------------------


def test_from_str_names_the_unexpected_component_not_the_input():
    with pytest.raises(ValueError) as caught:
        Source.from_str("sftp://user:S3CRET@h/path")
    assert "S3CRET" not in str(caught.value)
    assert "path" in str(caught.value)
    with pytest.raises(ValueError, match="query") as caught:
        Source.from_str("sftp://user:S3CRET@h?token=S3CRET")
    assert "S3CRET" not in str(caught.value)


def test_from_str_does_not_echo_a_password_when_the_port_is_wrong():
    with pytest.raises(ValueError) as caught:
        Source.from_str("sftp://user:S3CRET@h:99999")
    assert "S3CRET" not in str(caught.value)


def test_from_str_still_reads_an_authority():
    source = Source.from_str("http://user:pass@host:80")
    assert source == Source("http", "user:pass", "host", 80)
    assert Source.from_str("sftp://root@[::1]:22").port == 22
    assert Source.from_str("http://h/p", strict=False) == Source(
        "http", None, "h", None
    )


@pytest.mark.parametrize("scheme", ["sftp", "http", "ftp"])
def test_repr_and_str_of_a_source_hide_the_password(scheme):
    source = Uri(f"{scheme}://user:S3CRET@h/p").source
    assert "S3CRET" not in repr(source)
    assert "S3CRET" not in str(source)
    assert "user" in repr(source)


@pytest.mark.parametrize("scheme", ["github", "gitlab", "git", "git+github"])
def test_repr_and_str_of_a_source_hide_the_token_of_a_git_scheme(scheme):
    source = Uri(f"{scheme}://TOKEN123@github.com/o/r").source
    for text in (repr(source), str(source), repr((source,)), f"{source}"):
        assert "TOKEN123" not in text
    assert source.userinfo == "TOKEN123"


def test_a_token_scheme_in_a_source_keeps_its_host_in_the_text():
    source = Source("github", "TOKEN123", "github.com", None)
    assert str(source) == "github://github.com"
    assert source.as_str(sanitize=False) == "github://TOKEN123@github.com"


# --- a name that is not well-formed text -----------------------------------------------


def test_a_child_named_with_a_lone_surrogate_can_be_printed_hashed_and_sorted():
    base = UriPath("file:///tmp/dir")
    child = base / "a\ud800b.txt"
    other = base / "z"
    assert str(child) == "file:/tmp/dir/a%ED%A0%80b.txt"
    assert repr(child) == "FileUri('file:/tmp/dir/a%ED%A0%80b.txt')"
    assert hash(child) == hash(child)
    assert child == child
    assert {child, other} == {other, child}
    assert sorted([other, child]) == [child, other]


def test_a_surrogate_name_and_an_escaped_byte_each_keep_their_own_bytes():
    path = UriPath("file:///tmp") / "a\ud800\udcffb"
    assert str(path).endswith("a%ED%A0%80%FFb")


def test_the_text_of_a_surrogate_name_reads_back_as_the_same_text():
    child = UriPath("file:///tmp") / "a\ud800b.txt"
    assert Uri(child.as_uri()) == child
    assert UriPath(child.as_uri()).as_uri() == child.as_uri()


def test_a_non_utf8_escape_still_round_trips():
    path = UriPath("http://h/caf%E9.html")
    assert path.as_uri() == "http://h/caf%E9.html"
    assert (path.parent / path.name).as_uri() == "http://h/caf%E9.html"
