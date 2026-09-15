"""Data-loss regressions for HttpPath/DavPath against real loopback servers.

`dav_server` is wsgidav; the same server addressed through an `http:` URL
exercises wsgidav's own HTML directory listing, whose parent row is
`<a href="..">..</a>`.
"""

import errno
import functools
import http.server
import threading

import pytest

pytest.importorskip("requests")

from pathlib_next import LocalPath
from pathlib_next.uri.schemes.dav import DavPath
from pathlib_next.uri.schemes.http import HttpPath


def _http(dav_url):
    return "http" + dav_url[len("dav") :]


# --- httpdav-dotdot-listing-entry-deletes-parent ---


def test_http_listing_of_wsgidav_never_yields_dot_entries(dav_server, fixture_tree):
    sub = HttpPath(f"{_http(dav_server)}/sub/")
    children = list(sub.iterdir())
    assert sorted(c.name for c in children) == ["c.py", "nested"]
    assert all(c.path.rsplit("/", 1)[-1] not in (".", "..") for c in children)


def test_http_unlink_every_file_in_listing_keeps_parent(dav_server, fixture_tree):
    # The reported loop: with ".." listed as a file, its unlink() sent
    # DELETE /sub/.. -> DELETE / and wsgidav removed the whole share.
    sub = HttpPath(f"{_http(dav_server)}/sub/")
    for child in sub.iterdir():
        if child.is_file():
            child.unlink()
    assert not (fixture_tree / "sub" / "c.py").exists()
    assert (fixture_tree / "sub" / "nested" / "d.py").read_text() == "d"
    assert (fixture_tree / "a.txt").read_text() == "a"
    assert (fixture_tree / "empty_dir").is_dir()


def test_http_walk_of_wsgidav_terminates(dav_server):
    root = HttpPath(f"{_http(dav_server)}/sub/")
    tops = [top.path for top, _dirs, _files in root.walk()]
    assert len(tops) == 2
    assert all("/./" not in t and "/../" not in t for t in tops)


class _DotListingHandler(http.server.SimpleHTTPRequestHandler):
    """A `<pre>` index that lists "./", "../" and "%2E%2E/" as entries."""

    def list_directory(self, path):
        import io

        html = (
            "<html><head><title>Index of /d/</title></head><body><pre>"
            '<a href="./">./</a>\n<a href="../">../</a>\n'
            '<a href="%2E%2E/">%2E%2E/</a>\n<a href="x.txt">x.txt</a>\n'
            "</pre></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        return io.BytesIO(html)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def dot_listing_server(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x.txt").write_text("x")
    handler = functools.partial(_DotListingHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_pre_listing_dot_entries_are_dropped(dot_listing_server):
    d = HttpPath(f"{dot_listing_server}/d/")
    assert [c.name for c in d.iterdir()] == ["x.txt"]
    # "./" used to make walk() descend /d/./././... without end.
    tops = [top.path for top, _dirs, _files in d.walk()]
    assert tops == ["/d/"]


def test_dav_listing_of_wsgidav_has_no_dot_entries(dav_server):
    names = sorted(c.name for c in DavPath(f"{dav_server}/sub/").iterdir())
    assert names == ["c.py", "nested"]


# --- httpdav-unlink-deletes-collection-tree ---


def test_dav_unlink_on_collection_keeps_tree(dav_server, fixture_tree):
    p = DavPath(f"{dav_server}/sub")
    with pytest.raises(IsADirectoryError) as excinfo:
        p.unlink()
    assert excinfo.value.errno == errno.EISDIR
    with pytest.raises(IsADirectoryError):
        p.unlink(missing_ok=True)
    assert (fixture_tree / "sub" / "c.py").read_text() == "c"
    assert (fixture_tree / "sub" / "nested" / "d.py").read_text() == "d"


def test_dav_symlink_to_force_keeps_collection(dav_server, fixture_tree):
    p = DavPath(f"{dav_server}/sub")
    with pytest.raises(Exception):
        p.symlink_to("x", force=True)
    assert (fixture_tree / "sub" / "nested" / "d.py").read_text() == "d"


def test_dav_unlink_file_and_missing_semantics(dav_server, fixture_tree):
    DavPath(f"{dav_server}/a.txt").unlink()
    assert not (fixture_tree / "a.txt").exists()
    assert (fixture_tree / "b.py").exists()
    DavPath(f"{dav_server}/a.txt").unlink(missing_ok=True)
    with pytest.raises(FileNotFoundError):
        DavPath(f"{dav_server}/a.txt").unlink()


def test_dav_rmdir_and_recursive_rm_still_delete(dav_server, fixture_tree):
    DavPath(f"{dav_server}/empty_dir").rmdir()
    assert not (fixture_tree / "empty_dir").exists()
    DavPath(f"{dav_server}/sub").rm(recursive=True)
    assert not (fixture_tree / "sub").exists()
    assert (fixture_tree / "a.txt").exists()


def test_http_unlink_on_listed_directory_keeps_tree(dav_server, fixture_tree):
    root = HttpPath(f"{_http(dav_server)}/")
    sub = next(c for c in root.iterdir() if c.name == "sub")
    with pytest.raises(IsADirectoryError) as excinfo:
        sub.unlink()
    assert excinfo.value.errno == errno.EISDIR
    assert (fixture_tree / "sub" / "c.py").read_text() == "c"
    assert (fixture_tree / "sub" / "nested" / "d.py").read_text() == "d"


def test_http_unlink_refuses_redirecting_directory(http_server, fixture_tree):
    # The stdlib server 301s /sub -> /sub/, so stat() sees a directory;
    # unlink() must refuse before sending any DELETE.
    with pytest.raises(IsADirectoryError):
        HttpPath(f"{http_server}/sub").unlink()
    assert (fixture_tree / "sub" / "c.py").exists()


# --- httpdav-dav-read-ignores-http-status ---


def test_dav_read_missing_file_raises(dav_server, fixture_tree):
    p = DavPath(f"{dav_server}/missing.txt")
    with pytest.raises(FileNotFoundError):
        p.read_bytes()
    with pytest.raises(FileNotFoundError):
        p.open("rb")
    # Reads of existing files are unaffected, and the connection pool was
    # not exhausted by the failed streams above.
    for _ in range(3):
        with pytest.raises(FileNotFoundError):
            p.read_text()
    assert DavPath(f"{dav_server}/a.txt").read_text() == "a"


def test_dav_copy_missing_source_writes_no_error_page(dav_server, tmp_path):
    out = tmp_path / "out.txt"
    with pytest.raises(FileNotFoundError):
        DavPath(f"{dav_server}/missing.txt").copy(
            LocalPath(out), preserve_metadata=False
        )
    assert not out.exists() or b"<!DOCTYPE" not in out.read_bytes()
