"""Unit-only WebDAV tests: a fake `requests`-like session plugged into the
real `HttpBackend`, no real server. Covers PROPFIND-based stat/listdir and
PUT/DELETE/MKCOL/MOVE.
"""
import io

import pytest

requests = pytest.importorskip("requests")

from pathlib_next.uri.schemes.http import HttpBackend
from pathlib_next.uri.schemes.webdav import DavPath


class _FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.raw = io.BytesIO(content)
        self.is_redirect = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def close(self):
        pass


class _FakeSession:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "PROPFIND":
            key = (method, url, kwargs.get("headers", {}).get("Depth"))
        else:
            key = (method, url)
        resp = self.responses.get(key)
        if resp is None:
            return _FakeResponse(404)
        return resp() if callable(resp) else resp


def _dav(uri, session=None):
    return DavPath(uri, backend=HttpBackend(session or _FakeSession(), {}))


_MULTISTATUS_FILE = b"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/docs/readme.txt</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:getcontentlength>11</D:getcontentlength>
        <D:getlastmodified>Mon, 01 Jan 2026 12:00:00 GMT</D:getlastmodified>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""

_MULTISTATUS_DIR = b"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/docs/</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/></D:resourcetype>
        <D:getlastmodified>Mon, 01 Jan 2026 12:00:00 GMT</D:getlastmodified>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""

_MULTISTATUS_LISTING = b"""<?xml version="1.0"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/docs/</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/></D:resourcetype>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/docs/readme.txt</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:getcontentlength>11</D:getcontentlength>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/docs/sub/</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/></D:resourcetype>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""


def test_scheme_dispatch():
    assert isinstance(_dav("dav://host/docs/readme.txt"), DavPath)


def test_wire_uri_translates_scheme():
    assert _dav("dav://host/a")._wire_uri() == "http://host/a"
    assert _dav("davs://host/a")._wire_uri() == "https://host/a"


def test_stat_file():
    session = _FakeSession()
    session.responses[("PROPFIND", "http://host/docs/readme.txt", "0")] = _FakeResponse(
        207, _MULTISTATUS_FILE
    )
    st = _dav("dav://host/docs/readme.txt", session).stat()
    assert st.st_size == 11
    assert not st.is_dir()


def test_stat_dir():
    session = _FakeSession()
    session.responses[("PROPFIND", "http://host/docs/", "0")] = _FakeResponse(
        207, _MULTISTATUS_DIR
    )
    assert _dav("dav://host/docs/", session).stat().is_dir()


def test_stat_missing_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        _dav("dav://host/missing.txt").stat()


def test_is_dir_is_file():
    session = _FakeSession()
    session.responses[("PROPFIND", "http://host/docs/", "0")] = _FakeResponse(
        207, _MULTISTATUS_DIR
    )
    session.responses[("PROPFIND", "http://host/docs/readme.txt", "0")] = _FakeResponse(
        207, _MULTISTATUS_FILE
    )
    root = _dav("dav://host/docs/", session)
    assert root.is_dir()
    assert not root.is_file()
    f = _dav("dav://host/docs/readme.txt", session)
    assert f.is_file()
    assert not f.is_dir()


def test_listdir():
    session = _FakeSession()
    session.responses[("PROPFIND", "http://host/docs/", "1")] = _FakeResponse(
        207, _MULTISTATUS_LISTING
    )
    p = _dav("dav://host/docs/", session)
    assert sorted(p._listdir()) == ["readme.txt", "sub"]


def test_iterdir_yields_children():
    session = _FakeSession()
    session.responses[("PROPFIND", "http://host/docs/", "1")] = _FakeResponse(
        207, _MULTISTATUS_LISTING
    )
    p = _dav("dav://host/docs/", session)
    assert sorted(c.name for c in p.iterdir()) == ["readme.txt", "sub"]


def test_open_read_downloads_via_get():
    session = _FakeSession()
    session.responses[("GET", "http://host/docs/readme.txt")] = _FakeResponse(
        200, b"hello world"
    )
    p = _dav("dav://host/docs/readme.txt", session)
    assert p.read_bytes() == b"hello world"


def test_open_write_uploads_via_put_on_close():
    session = _FakeSession()
    session.responses[("PUT", "http://host/docs/new.txt")] = _FakeResponse(201)
    p = _dav("dav://host/docs/new.txt", session)
    p.write_bytes(b"new content")
    method, url, kwargs = session.calls[-1]
    assert method == "PUT"
    assert kwargs["data"] == b"new content"


def test_mkdir_sends_mkcol():
    session = _FakeSession()
    session.responses[("MKCOL", "http://host/docs/newdir")] = _FakeResponse(201)
    p = _dav("dav://host/docs/newdir", session)
    p._mkdir(0o777)
    assert session.calls[-1][:2] == ("MKCOL", "http://host/docs/newdir")


def test_mkdir_existing_raises_file_exists():
    session = _FakeSession()
    session.responses[("MKCOL", "http://host/docs/newdir")] = _FakeResponse(405)
    p = _dav("dav://host/docs/newdir", session)
    with pytest.raises(FileExistsError):
        p._mkdir(0o777)


def test_unlink_sends_delete():
    session = _FakeSession()
    session.responses[("DELETE", "http://host/docs/readme.txt")] = _FakeResponse(204)
    p = _dav("dav://host/docs/readme.txt", session)
    p.unlink()
    assert session.calls[-1][:2] == ("DELETE", "http://host/docs/readme.txt")


def test_unlink_missing_ok():
    session = _FakeSession()
    session.responses[("DELETE", "http://host/missing.txt")] = _FakeResponse(404)
    _dav("dav://host/missing.txt", session).unlink(missing_ok=True)


def test_unlink_missing_without_missing_ok_raises():
    session = _FakeSession()
    session.responses[("DELETE", "http://host/missing.txt")] = _FakeResponse(404)
    p = _dav("dav://host/missing.txt", session)
    with pytest.raises(FileNotFoundError):
        p.unlink()


def test_rename_sends_move_with_destination_header():
    session = _FakeSession()
    session.responses[("MOVE", "http://host/docs/a.txt")] = _FakeResponse(201)
    p = _dav("dav://host/docs/a.txt", session)
    p.rename("b.txt")
    method, url, kwargs = session.calls[-1]
    assert method == "MOVE"
    assert kwargs["headers"]["Destination"] == "http://host/docs/b.txt"


def test_chmod_not_implemented():
    p = _dav("dav://host/docs/a.txt")
    with pytest.raises(NotImplementedError):
        p.chmod(0o644)
