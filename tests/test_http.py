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
    def __init__(self, status_code, text="", headers=None, url=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}
        self.url = url or "http://example.com/file.txt"
        self.is_redirect = status_code in (301, 302, 303, 307, 308)
        self.reason = "OK" if status_code < 400 else "Not Found"

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(response=self)

    def close(self):
        pass


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


# --- Append mode tests (Phase 1) ---

def test_http_append_rewrite_mode_nonexistent_file(http_writable_server):
    """Append to non-existent file in rewrite mode (default).
    Should behave like write mode."""
    p = UriPath(f"{http_writable_server}/append_new.txt")
    with p.open("ab") as f:
        f.write(b"hello")
    assert p.read_bytes() == b"hello"


def test_http_append_rewrite_mode_existing_file(http_writable_server):
    """Append to existing file in rewrite mode (default).
    Should GET existing, then PUT old+new."""
    p = UriPath(f"{http_writable_server}/append_existing.txt")
    # Write initial content
    p.write_bytes(b"hello")
    # Append to it
    with p.open("ab") as f:
        f.write(b" world")
    assert p.read_bytes() == b"hello world"


def test_http_append_rewrite_mode_multiple_writes(http_writable_server):
    """Multiple writes in append mode should concatenate."""
    p = UriPath(f"{http_writable_server}/append_multi.txt")
    p.write_bytes(b"a")
    with p.open("ab") as f:
        f.write(b"b")
        f.write(b"c")
    assert p.read_bytes() == b"abc"


def test_http_append_patch_mode_configured(monkeypatch):
    """Test PATCH mode with Content-Range header."""
    import requests

    recorded = []

    def mock_request(self, method, url, **kwargs):
        recorded.append((method, url, kwargs.get("headers", {}), kwargs.get("data")))
        if method == "HEAD":
            return _MockHttpResponse(200, "", headers={"Content-Length": "0"}, url=url)
        elif method == "GET":
            return _MockHttpResponse(200, "existing content", url=url)
        elif method == "PATCH":
            return _MockHttpResponse(204, url=url)
        else:
            return _MockHttpResponse(200, url=url)

    monkeypatch.setattr(requests.Session, "request", mock_request)

    p = UriPath("http://example.com/file.txt")
    session = requests.Session()
    p = p.with_session(session, append_mode="patch")

    with p.open("ab") as f:
        f.write(b"new content")

    # Should have made a STAT call (to get size) and a PATCH call
    patch_calls = [r for r in recorded if r[0] == "PATCH"]
    assert len(patch_calls) > 0
    # Content-Range header should be present
    assert any("Content-Range" in r[2] for r in patch_calls)


def test_http_append_patch_mode_rejection(monkeypatch):
    """PATCH mode should raise if server rejects with 405."""
    import requests

    def mock_request(self, method, url, **kwargs):
        if method == "PATCH":
            return _MockHttpResponse(405)
        elif method == "HEAD":
            return _MockHttpResponse(200, "", headers={"Content-Length": "15"})
        elif method == "GET":
            return _MockHttpResponse(200, "existing content")
        else:
            return _MockHttpResponse(200)

    monkeypatch.setattr(requests.Session, "request", mock_request)

    p = UriPath("http://example.com/file.txt")
    session = requests.Session()
    p = p.with_session(session, append_mode="patch")

    # Should raise PermissionError on PATCH rejection (405 → PermissionError)
    with pytest.raises(PermissionError):
        with p.open("ab") as f:
            f.write(b"new content")


def test_http_append_write_stream_marks_closed_on_failure(monkeypatch):
    """Append stream must mark itself closed even on failed close()."""
    import requests
    from pathlib_next.uri.schemes.http import HttpAppendStream

    class MockResponse:
        def __init__(self, status_code=500):
            self.status_code = status_code
            self.reason = "Server Error" if status_code >= 400 else "OK"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.exceptions.HTTPError(response=self)

    call_count = [0]

    def mock_request(self, method, url, **kwargs):
        call_count[0] += 1
        # First call (GET for initial content in rewrite mode) succeeds
        if call_count[0] == 1:
            return MockResponse(404)  # File doesn't exist
        # Second call (PUT to close) fails
        return MockResponse(500)

    monkeypatch.setattr(requests.Session, "request", mock_request)

    p = UriPath("http://example.com/f.txt")
    stream = HttpAppendStream(p)
    stream.write(b"x")

    with pytest.raises(OSError):
        stream.close()
    assert stream.closed

    # Second close() must be a no-op
    stream.close()


def test_http_append_default_is_rewrite(monkeypatch):
    """Default append_mode should be 'rewrite'."""
    from pathlib_next.uri.schemes.http import HttpBackend
    import requests

    backend = HttpBackend(
        session=requests.Session(),
        requests_args={},
        write_method="PUT"
    )
    assert backend.append_mode == "rewrite"
