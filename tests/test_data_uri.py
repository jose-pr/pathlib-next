"""`data:` scheme (RFC 2397): read-only, no server/backend involved."""

import pytest

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.data import DataUri


def test_scheme_dispatch():
    p = UriPath("data:text/plain,hello")
    assert isinstance(p, DataUri)


def test_base64_decodes_content():
    p = UriPath("data:text/plain;base64,SGVsbG8sIFdvcmxkIQ==")
    assert p.read_bytes() == b"Hello, World!"
    assert p.read_text() == "Hello, World!"


def test_percent_encoded_decodes_content():
    p = UriPath("data:text/plain,Hello%2C%20World%21")
    assert p.read_text() == "Hello, World!"


def test_mediatype_explicit():
    p = UriPath("data:image/png;base64,AAAA")
    assert p.mediatype == "image/png"


def test_mediatype_default_when_omitted():
    p = UriPath("data:,hello")
    assert p.mediatype == "text/plain;charset=US-ASCII"


def test_mediatype_with_params_and_base64():
    p = UriPath("data:text/plain;charset=utf-8;base64,aGk=")
    assert p.mediatype == "text/plain;charset=utf-8"
    assert p.read_text() == "hi"


def test_stat_size_matches_decoded_content():
    p = UriPath("data:text/plain;base64,SGVsbG8=")  # "Hello"
    assert p.stat().st_size == 5


def test_exists_is_file_not_dir():
    p = UriPath("data:text/plain,hi")
    assert p.exists()
    assert p.is_file()
    assert not p.is_dir()


def test_iterdir_raises_not_a_directory():
    p = UriPath("data:text/plain,hi")
    with pytest.raises(NotADirectoryError):
        list(p.iterdir())


def test_missing_comma_raises_file_not_found():
    p = UriPath("data:text/plain;base64")
    with pytest.raises(FileNotFoundError):
        p.read_bytes()


@pytest.mark.parametrize(
    "op",
    [
        lambda p: p.write_text("x"),
        lambda p: p.mkdir(),
        lambda p: p.unlink(),
        lambda p: p.chmod(0o644),
    ],
)
def test_write_operations_raise_not_implemented(op):
    p = UriPath("data:text/plain,hi")
    with pytest.raises(NotImplementedError):
        op(p)


@pytest.mark.parametrize(
    "url, content",
    [
        ("data:,a?b", b"a?b"),
        ("data:,a?b=c&d", b"a?b=c&d"),
        (
            'data:image/svg+xml,<?xml version="1.0"?><svg/>',
            b'<?xml version="1.0"?><svg/>',
        ),
        ("data:,a%3Fb?c%20d", b"a?b?c d"),
        ("data:text/plain;base64,YWJj", b"abc"),
        ("data:,a#b", b"a"),
        ("data:,a?b#c", b"a?b"),
    ],
)
def test_a_question_mark_in_a_data_payload_is_payload(url, content):
    path = UriPath(url)
    assert path.read_bytes() == content
    assert path.stat().st_size == len(content)


def test_a_data_payload_with_a_query_reads_the_whole_text():
    path = UriPath("data:,a?b")
    assert path.read_text() == "a?b"
    assert path.query == "b"
    assert path.as_uri() == "data:,a?b"
    assert path.stat().st_size == 3
