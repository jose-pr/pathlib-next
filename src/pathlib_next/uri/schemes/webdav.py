from __future__ import annotations

import io as _io
import stat as _stat
import urllib.parse as _urlparse
import xml.etree.ElementTree as _ET

import uritools as _uritools

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import Uri
from .http import HttpPath

_NS = {"D": "DAV:"}

_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:resourcetype/>
    <D:getcontentlength/>
    <D:getlastmodified/>
  </D:prop>
</D:propfind>"""


def _parse_response(elem) -> "tuple[str, bool, int, str]":
    href = elem.findtext("D:href", namespaces=_NS) or ""
    prop = elem.find("D:propstat/D:prop", _NS)
    resourcetype = prop.find("D:resourcetype", _NS) if prop is not None else None
    is_dir = (
        resourcetype is not None
        and resourcetype.find("D:collection", _NS) is not None
    )
    size_text = prop.findtext("D:getcontentlength", namespaces=_NS) if prop is not None else None
    size = int(size_text) if size_text else 0
    lm = prop.findtext("D:getlastmodified", namespaces=_NS) if prop is not None else None
    return _urlparse.unquote(href), is_dir, size, lm


class _DavWriteStream(_io.BytesIO):
    def __init__(self, path: "DavPath"):
        super().__init__()
        self._path = path

    def close(self):
        if not self.closed:
            resp = self._path.backend.request(
                "PUT", self._path._wire_uri(), data=self.getvalue()
            )
            resp.raise_for_status()
        super().close()


class DavPath(HttpPath):
    """`dav:`/`davs:` scheme: WebDAV (RFC 4918) over HTTP(S). Extends
    `HttpPath` with PROPFIND (stat/listdir -- real directory metadata,
    replacing `HttpPath`'s HTML-index scraping) and PUT/DELETE/MKCOL/MOVE
    (full write support). Requests go to the equivalent `http:`/`https:`
    URL (`_wire_uri()`); `as_uri()` still reports `dav:`/`davs:`. Reuses
    `HttpPath`'s `HttpBackend` (session + requests_args) and the `http`
    extra -- no new dependency.

    Caution: `rmdir()` maps to WebDAV `DELETE`, which is recursive by
    spec (RFC 4918) -- unlike `pathlib.Path.rmdir()`, it does NOT require
    the collection to be empty first.
    """

    __SCHEMES = ("dav", "davs")
    __slots__ = ()

    def _wire_uri(self) -> str:
        scheme = "https" if self.source.scheme == "davs" else "http"
        return _uritools.uricompose(
            scheme=scheme,
            userinfo=self.source.userinfo,
            host=self.source.host,
            port=self.source.port,
            path=self.path,
            query=self.query or None,
            fragment=self.fragment or None,
        )

    def _propfind(self, depth="0"):
        resp = self.backend.request(
            "PROPFIND",
            self._wire_uri(),
            headers={"Depth": depth, "Content-Type": "application/xml"},
            data=_PROPFIND_BODY,
        )
        if resp.status_code == 404:
            raise FileNotFoundError(self)
        if resp.status_code == 403:
            raise PermissionError(self)
        resp.raise_for_status()
        return _ET.fromstring(resp.content)

    def stat(self, *, follow_symlinks=True):
        root = self._propfind(depth="0")
        responses = root.findall("D:response", _NS)
        if not responses:
            raise FileNotFoundError(self)
        _href, is_dir, size, lm = _parse_response(responses[0])
        return FileStat(st_size=size, st_mtime=_utils.parsedate(lm), is_dir=is_dir)

    def is_dir(self):
        # Shadow HttpPath.is_dir()'s _isdir-slot cache (never populated by
        # our stat() override) -- derive from stat() like any other scheme.
        return _stat.S_ISDIR(self._st_mode() or 0)

    def is_file(self):
        return _stat.S_ISREG(self._st_mode() or 0)

    def _listdir(self):
        root = self._propfind(depth="1")
        self_path = _urlparse.urlsplit(self._wire_uri()).path.rstrip("/")
        for elem in root.findall("D:response", _NS):
            href, _is_dir, _size, _lm = _parse_response(elem)
            href_path = _urlparse.urlsplit(href).path.rstrip("/")
            if not href_path or href_path == self_path:
                continue  # the "." entry describing self, per RFC 4918
            name = href_path.rsplit("/", 1)[-1]
            if name:
                yield name

    def iterdir(self):
        # Reinstate the plain name+_make_child_relpath contract (HttpPath
        # overrides this to expect FileEntry-like objects from _listdir(),
        # which ours doesn't return).
        for name in self._listdir():
            yield self._make_child_relpath(name)

    def _open(self, mode="r", buffering=-1):
        if "r" in mode:
            buffer_size = _io.DEFAULT_BUFFER_SIZE if buffering < 0 else buffering
            req = self.backend.request("GET", self._wire_uri(), stream=True)
            resp = req.raw
            resp.auto_close = False
            return (
                resp
                if buffer_size == 0
                else _io.BufferedReader(resp, buffer_size=buffer_size)
            )
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _DavWriteStream(self)

    def _mkdir(self, mode):
        resp = self.backend.request("MKCOL", self._wire_uri())
        if resp.status_code == 409:
            raise FileNotFoundError(self)
        if resp.status_code in (405, 501):
            raise FileExistsError(self)
        resp.raise_for_status()

    def unlink(self, missing_ok=False):
        resp = self.backend.request("DELETE", self._wire_uri())
        if resp.status_code == 404:
            if missing_ok:
                return
            raise FileNotFoundError(self)
        resp.raise_for_status()

    def rmdir(self):
        self.unlink()

    def rename(self, target: "DavPath | Uri | str"):
        if not isinstance(target, Uri):
            target = Uri(self.parent, target)
        dest = self.with_path(target.path)._wire_uri()
        resp = self.backend.request(
            "MOVE", self._wire_uri(), headers={"Destination": dest, "Overwrite": "F"}
        )
        if resp.status_code == 404:
            raise FileNotFoundError(self)
        if resp.status_code == 412:
            raise FileExistsError(target)
        resp.raise_for_status()
