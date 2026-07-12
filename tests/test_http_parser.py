"""Unit tests for `_DirectoryListingParser` (the in-house zero-dependency
HTML directory-listing parser) fed synthetic Apache `<pre>` / nginx
`<table>` HTML directly -- no server/fixture needed. `tests/test_http.py`
only ever drives this parser's `all_links` fallback branch (the real
`http_server` fixture serves stdlib `SimpleHTTPRequestHandler`'s bare
`<ul><li>` listing), so these tests are what actually exercises the
Apache/nginx-format code paths.
"""
import time

import pytest

pytest.importorskip("requests")

from pathlib_next.uri.schemes.http import _DirectoryListingParser, _human2bytes


def parse(html):
    parser = _DirectoryListingParser()
    parser.feed(html)
    parser.close()
    return parser.listing


def apache_pre(*rows, cwd="/files/"):
    body = "".join(rows)
    return (
        f"<html><head><title>Index of {cwd}</title></head><body>"
        f"<h1>Index of {cwd}</h1><pre>"
        f'<a href="../">../</a>\n'
        f"{body}</pre></body></html>"
    )


def pre_row(href, trailing_text, name=None):
    name = name or href
    return f'<a href="{href}">{name}</a>{trailing_text}\n'


def nginx_table(*rows, header="Name|Last modified|Size|Description"):
    cols = header.split("|")
    head = "<tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>\n"
    parent = (
        '<tr><td><a href="/files/">Parent Directory</a></td>'
        "<td>&nbsp;</td><td align=\"right\">-</td><td>&nbsp;</td></tr>\n"
    )
    body = "".join(rows)
    return (
        "<html><head><title>Index of /files/</title></head><body>"
        "<h1>Index of /files/</h1><table>\n"
        + head
        + parent
        + body
        + "</table></body></html>"
    )


def table_row(href, name, modified="", size="", description="&nbsp;"):
    return (
        f'<tr><td><a href="{href}">{name}</a></td>'
        f"<td>{modified}</td><td>{size}</td><td>{description}</td></tr>\n"
    )


# --- Phase 1: _DATETIME_FMTs, one representative string per bucket ---

_DATETIME_CASES = [
    ("11-Jul-2026 10:23:00", "%d-%b-%Y %H:%M:%S"),
    ("11-Jul-2026 10:23", "%d-%b-%Y %H:%M"),
    ("2026-07-11 10:23:00", "%Y-%m-%d %H:%M:%S"),
    ("2026-07-11T10:23:00Z", "%Y-%m-%dT%H:%M:%SZ"),
    ("2026-07-11 10:23", "%Y-%m-%d %H:%M"),
    ("2026-Jul-11 10:23:00", "%Y-%b-%d %H:%M:%S"),
    ("2026-Jul-11 10:23", "%Y-%b-%d %H:%M"),
    ("Sat Jul 11 10:23:00 2026", "%a %b %d %H:%M:%S %Y"),
    ("Sat, 11 Jul 2026 10:23:00 GMT", "%a, %d %b %Y %H:%M:%S %Z"),
    ("2026-07-11", "%Y-%m-%d"),
    ("11/07/2026 10:23:00 +0000", "%d/%m/%Y %H:%M:%S %z"),
    ("11 Jul 2026", "%d %b %Y"),
]


@pytest.mark.parametrize("text,fmt", _DATETIME_CASES, ids=[c[1] for c in _DATETIME_CASES])
def test_pre_datetime_format_buckets(text, fmt):
    html = apache_pre(pre_row("a.txt", f"  {text}  1.0K"))
    entry = next(e for e in parse(html) if e.name == "a.txt")
    expected = time.strptime(text, fmt)
    assert entry.modified is not None
    assert entry.modified[:6] == expected[:6]


# --- _RE_FILESIZE / _human2bytes ---

def test_human2bytes_plain_int():
    assert _human2bytes("42") == 42


def test_human2bytes_dash_is_none_via_parser():
    html = apache_pre(pre_row("a.txt", "  11-Jul-2026 10:23   -"))
    entry = next(e for e in parse(html) if e.name == "a.txt")
    assert entry.size is None


@pytest.mark.parametrize(
    "sizestr,expected",
    [
        ("1K", 1024),
        ("1.5K", int(1.5 * 1024)),
        ("2M", 2 * 1024**2),
        ("1G", 1024**3),
    ],
)
def test_human2bytes_units(sizestr, expected):
    assert _human2bytes(sizestr) == expected


def test_pre_size_decimal_and_comma():
    html = apache_pre(
        pre_row("a.txt", "  11-Jul-2026 10:23   1.5K"),
        pre_row("b.txt", "  11-Jul-2026 10:24   1,024"),
    )
    listing = {e.name: e for e in parse(html)}
    assert listing["a.txt"].size == int(1.5 * 1024)
    assert listing["b.txt"].size == 1024


# --- _process_table header classification ---

def test_table_header_name_modified_size_description():
    html = nginx_table(
        table_row("a.txt", "a.txt", "2026-07-11 10:23", "1.0K", "a file"),
    )
    entry = next(e for e in parse(html) if e.name == "a.txt")
    assert entry.modified is not None
    assert entry.size == int(1.0 * 1024)
    assert entry.description == "a file"


def test_table_header_signature_column_falls_back_to_description():
    html = nginx_table(
        table_row("a.txt", "a.txt", "2026-07-11 10:23", "1.0K", "sig-value"),
        header="Name|Last modified|Size|Signature",
    )
    entry = next(e for e in parse(html) if e.name == "a.txt")
    # unrecognized trailing column (classified as "signature") is dropped;
    # only name/modified/size are populated.
    assert entry.size == int(1.0 * 1024)


def test_table_header_unrecognized_column_treated_as_description():
    html = nginx_table(
        table_row("a.txt", "a.txt", "2026-07-11 10:23", "1.0K", "extra-stuff"),
        header="Name|Last modified|Size|Type",
    )
    entry = next(e for e in parse(html) if e.name == "a.txt")
    assert entry.description == "extra-stuff"


def test_table_row_skips_parent_directory():
    html = nginx_table(table_row("a.txt", "a.txt"))
    names = [e.name for e in parse(html)]
    assert "Parent Directory" not in names
    assert ".." not in names


# --- _flush_pre_entry: Parent Directory / '..' / absolute-or-query href skip ---

def test_pre_skips_parent_directory_variants():
    html = apache_pre(
        pre_row("?C=N;O=D", "  ", name="Name"),
        pre_row("/absolute/elsewhere", "  11-Jul-2026 10:23   1K", name="elsewhere"),
        pre_row("a.txt", "  11-Jul-2026 10:23   1K"),
    )
    names = [e.name for e in parse(html)]
    assert names == ["a.txt"]


def test_pre_absolute_href_child_entries_kept():
    # A reverse-proxied/absolute-URL-configured server can render every
    # entry (not just the parent link) as an absolute href -- a blanket
    # startswith('/') filter would drop the whole listing (regression:
    # this used to come back empty).
    html = (
        "<html><head><title>Index of /files/</title></head><body><pre>"
        '<a href="/files/">../</a>\n'
        '<a href="/files/a.txt">a.txt</a>  11-Jul-2026 10:23   1K\n'
        '<a href="/files/sub/">sub/</a>  11-Jul-2026 10:24   -\n'
        "</pre></body></html>"
    )
    names = {e.name for e in parse(html)}
    assert names == {"a.txt", "sub/"}


def test_pre_absolute_href_parent_link_dropped_without_title():
    # No <title> to scope against -- falls back to the old conservative
    # (drop all absolute hrefs) behavior rather than risk leaking the
    # parent link as a fake entry.
    html = (
        "<html><body><pre>"
        '<a href="/files/">../</a>\n'
        '<a href="/files/a.txt">a.txt</a>  11-Jul-2026 10:23   1K\n'
        "</pre></body></html>"
    )
    assert parse(html) == []


def test_pre_directory_entry_has_trailing_slash():
    html = apache_pre(pre_row("sub/", "  11-Jul-2026 10:23   -"))
    entry = next(e for e in parse(html) if e.name == "sub/")
    assert entry.name.endswith("/")


# --- close()'s all_links fallback (no <pre>/<table> at all) ---

def test_all_links_fallback_branch():
    html = (
        "<html><body><ul>"
        '<li><a href="../">../</a></li>'
        '<li><a href="a.txt">a.txt</a></li>'
        '<li><a href="sub/">sub/</a></li>'
        '<li><a href="?C=N">Name</a></li>'
        '<li><a href="/absolute">absolute</a></li>'
        "</ul></body></html>"
    )
    listing = parse(html)
    names = {e.name for e in listing}
    assert names == {"a.txt", "sub/"}
    # fallback branch carries no metadata
    assert all(e.modified is None and e.size is None and e.description is None for e in listing)


def test_all_links_fallback_not_used_when_pre_present():
    # A <pre> listing (even with zero real entries after skip-filtering)
    # must not fall through to the all_links path -- close() only uses
    # all_links "if not self.listing".
    html = apache_pre()  # only the '../' entry, filtered out
    assert parse(html) == []
