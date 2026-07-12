"""HttpPath against a real stdlib ThreadingHTTPServer (see conftest.py's
http_server fixture) serving fixture_tree's directory listing. Skipped
entirely if the http extra isn't installed.
"""
import pytest

pytest.importorskip("requests")

from pathlib_next.uri import UriPath


def test_iterdir_lists_files_and_dirs(http_server):
    root = UriPath(f"{http_server}/")
    names = {p.name for p in root.iterdir()}
    # B-adjacent regression: subdirectory entries used to get name == ""
    # because directory-listing entries carry a trailing "/" that wasn't
    # stripped before deriving .name. iterdir() (unlike glob()) doesn't
    # filter hidden entries, matching pathlib.Path.iterdir().
    assert names == {"a.txt", "b.py", ".hidden.txt", "sub", "empty_dir"}


def test_iterdir_subdir_is_dir_not_file(http_server):
    root = UriPath(f"{http_server}/")
    sub = next(p for p in root.iterdir() if p.name == "sub")
    assert sub.is_dir()
    assert not sub.is_file()


def test_file_is_file_not_dir(http_server):
    root = UriPath(f"{http_server}/")
    f = next(p for p in root.iterdir() if p.name == "a.txt")
    assert f.is_file()
    assert not f.is_dir()


def test_read_text(http_server):
    p = UriPath(f"{http_server}/a.txt")
    assert p.read_text() == "a"


def test_stat_size(http_server):
    p = UriPath(f"{http_server}/a.txt")
    st = p.stat()
    assert st.st_size == 1  # fixture_tree's a.txt content is "a"


def test_stat_missing_raises_filenotfounderror(http_server):
    p = UriPath(f"{http_server}/does-not-exist.txt")
    with pytest.raises(FileNotFoundError):
        p.stat()


def test_exists_false_for_missing(http_server):
    p = UriPath(f"{http_server}/does-not-exist.txt")
    assert not p.exists()


def test_dir_without_trailing_slash_is_dir(http_server):
    # The server 301-redirects a bare "/sub" request to "/sub/" --
    # is_dir() must follow that and report True.
    p = UriPath(f"{http_server}/sub")
    assert p.is_dir()


def test_recursive_glob(http_server):
    root = UriPath(f"{http_server}/")
    names = {p.name for p in root.glob("**/*.py")}
    assert names == {"b.py", "c.py", "d.py"}

def test_http_exception_translation(http_server):
    # Test standard exception translation on real HTTP errors.
    # 404 client error -> FileNotFoundError
    with pytest.raises(FileNotFoundError):
        UriPath(f"{http_server}/does-not-exist.txt").read_text()

    # 405 Method Not Allowed / 501 Not Implemented -> PermissionError or NotImplementedError.
    # The stdlib ThreadingHTTPServer returns 501 or 405 for DELETE/PUT.
    p = UriPath(f"{http_server}/a.txt")
    with pytest.raises(PermissionError):
        p.unlink()


def test_http_write_put_default(monkeypatch):
    recorded = []

    class MockResponse:
        status_code = 200
        content = b""
        reason = "OK"

        def raise_for_status(self):
            pass

    def mock_request(self, method, url, **kwargs):
        recorded.append((method, url, kwargs.get("data")))
        return MockResponse()

    import requests
    monkeypatch.setattr(requests.Session, "request", mock_request)

    p = UriPath("http://example.com/file.txt")
    p.write_text("hello")

    assert len(recorded) == 1
    assert recorded[0] == ("PUT", "http://example.com/file.txt", b"hello")


def test_http_write_post_configured(monkeypatch):
    recorded = []

    class MockResponse:
        status_code = 200
        content = b""
        reason = "OK"

        def raise_for_status(self):
            pass

    def mock_request(self, method, url, **kwargs):
        recorded.append((method, url, kwargs.get("data")))
        return MockResponse()

    import requests
    monkeypatch.setattr(requests.Session, "request", mock_request)

    # Configure session with POST as the write method
    p = UriPath("http://example.com/file.txt")
    p = p.with_session(requests.Session(), write_method="POST")
    p.write_text("world")

    assert len(recorded) == 1
    assert recorded[0] == ("POST", "http://example.com/file.txt", b"world")


def test_http_unlink(monkeypatch):
    recorded = []

    class MockResponse:
        status_code = 200
        content = b""
        reason = "OK"

        def raise_for_status(self):
            pass

    def mock_request(self, method, url, **kwargs):
        recorded.append((method, url))
        return MockResponse()

    import requests
    monkeypatch.setattr(requests.Session, "request", mock_request)

    p = UriPath("http://example.com/file.txt")
    p.unlink()

    assert len(recorded) == 1
    assert recorded[0] == ("DELETE", "http://example.com/file.txt")


def test_http_rmdir_empty(monkeypatch):
    recorded = []
    from pathlib_next.uri.schemes.http import HttpPath

    class MockResponse:
        status_code = 200
        content = b""
        text = ""
        reason = "OK"

        def raise_for_status(self):
            pass

    def mock_request(self, method, url, **kwargs):
        recorded.append((method, url))
        return MockResponse()

    import requests
    monkeypatch.setattr(requests.Session, "request", mock_request)
    monkeypatch.setattr(HttpPath, "_listdir", lambda self: [])
    monkeypatch.setattr(HttpPath, "is_dir", lambda self: True)

    p = UriPath("http://example.com/dir/")
    p.rmdir()

    assert len(recorded) == 1
    assert recorded[0] == ("DELETE", "http://example.com/dir/")


def test_http_rmdir_not_empty(monkeypatch):
    import errno
    from pathlib_next.uri.schemes.http import HttpPath, _FileEntry

    monkeypatch.setattr(
        HttpPath, "_listdir", lambda self: [_FileEntry("a.txt", None, None, None)]
    )
    monkeypatch.setattr(HttpPath, "is_dir", lambda self: True)

    p = UriPath("http://example.com/dir/")
    with pytest.raises(OSError) as excinfo:
        p.rmdir()
    assert excinfo.value.errno == errno.ENOTEMPTY


def test_http_rmdir_on_file_raises_notadirectoryerror(monkeypatch):
    # rmdir() on a *file* must raise NotADirectoryError (matching
    # os.rmdir()'s ENOTDIR contract), not silently DELETE it -- an empty
    # directory's listing and a file whose body parses to zero entries are
    # indistinguishable from _listdir() alone.
    from pathlib_next.uri.schemes.http import HttpPath

    monkeypatch.setattr(HttpPath, "is_dir", lambda self: False)

    p = UriPath("http://example.com/file.txt")
    with pytest.raises(NotADirectoryError):
        p.rmdir()


def test_http_write_stream_marks_closed_on_upload_failure(monkeypatch):
    # A failed close() must not leave the stream open -- otherwise a
    # later close() (context-manager __exit__, or GC via
    # IOBase.__del__) silently retries the PUT.
    import requests
    from pathlib_next.uri.schemes.http import HttpWriteStream

    calls = []

    class MockResponse:
        status_code = 500
        reason = "Server Error"

        def raise_for_status(self):
            raise requests.exceptions.HTTPError(response=self)

    def mock_request(self, method, url, **kwargs):
        calls.append((method, url))
        return MockResponse()

    monkeypatch.setattr(requests.Session, "request", mock_request)

    p = UriPath("http://example.com/f.txt")
    stream = HttpWriteStream(p)
    stream.write(b"x")

    with pytest.raises(OSError):
        stream.close()
    assert stream.closed

    # a second close() must be a no-op, not another PUT attempt
    stream.close()
    assert len(calls) == 1


class _MockHttpResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.reason = "OK" if status_code < 400 else "Not Found"

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(response=self)


def test_listdir_retries_with_trailing_slash_on_404(monkeypatch):
    # Real servers (incl. this repo's http_server fixture) 301-redirect a
    # slash-less directory GET, and requests follows that automatically --
    # but a non-redirecting server/proxy would just 404 the bare path. This
    # mirrors stat()'s [no-slash, with-slash] retry so _listdir() is
    # robust to that case too (Phase 2 decision: (b), kept minimal).
    import requests

    listing_html = (
        "<html><body><pre>"
        '<a href="../">../</a>\n'
        '<a href="a.txt">a.txt</a>  11-Jul-2026 10:23   1K\n'
        "</pre></body></html>"
    )

    def mock_request(self, method, url, **kwargs):
        if url.endswith("/"):
            return _MockHttpResponse(200, listing_html)
        return _MockHttpResponse(404)

    monkeypatch.setattr(requests.Session, "request", mock_request)

    p = UriPath("http://example.com/sub")
    names = {e.name for e in p._listdir()}
    assert names == {"a.txt"}


def test_listdir_no_retry_when_already_trailing_slash(monkeypatch):
    # A 404 on a path that already ends in "/" is a real 404, not a
    # missing-redirect case -- must not retry (would just repeat the same
    # request) and must propagate FileNotFoundError.
    import requests

    calls = []

    def mock_request(self, method, url, **kwargs):
        calls.append(url)
        return _MockHttpResponse(404)

    monkeypatch.setattr(requests.Session, "request", mock_request)

    p = UriPath("http://example.com/sub/")
    with pytest.raises(FileNotFoundError):
        p._listdir()
    assert calls == ["http://example.com/sub/"]
