from __future__ import annotations

import errno as _errno
import io as _io
import typing as _ty
import urllib.parse as _urlparse
import xml.etree.ElementTree as _ET

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import Uri
from ..source import _compose_uri
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

    `rmdir()` enforces pathlib's "must be empty" contract with a depth-1
    PROPFIND before DELETE (WebDAV `DELETE` is recursive by spec, RFC 4918,
    unlike `pathlib.Path.rmdir()`). The native recursive DELETE is still
    available -- and cheaper than the base class's client-side walk -- via
    `rm(recursive=True)`, overridden below to issue a single request.
    """

    __SCHEMES = ("dav", "davs")
    __slots__ = ()

    def _wire_uri(self) -> str:
        # Direct string assembly (uri_parse_perf.md Phase 2) instead of
        # uricompose() -- called on every DAV HTTP request, and every
        # component here already came from this instance's own parsed
        # source/path/query/fragment state. See source.py's _compose_uri.
        scheme = "https" if self.source.scheme == "davs" else "http"
        return _compose_uri(
            scheme,
            self.source.userinfo,
            self.source.host,
            self.source.port,
            self.path,
            self.query or None,
            self.fragment or None,
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
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        root = self._propfind(depth="0")
        responses = root.findall("D:response", _NS)
        if not responses:
            raise FileNotFoundError(self)
        _href, is_dir, size, lm = _parse_response(responses[0])
        return FileStat(st_size=size, st_mtime=_utils.parsedate(lm), is_dir=is_dir)

    def _scandir(self):
        # One PROPFIND (Depth: 1) already carries type/size/mtime for every
        # child -- reuse it instead of `iterdir()` + a stat per child.
        root = self._propfind(depth="1")
        self_path = _urlparse.urlsplit(self._wire_uri()).path.rstrip("/")
        for elem in root.findall("D:response", _NS):
            href, is_dir, size, lm = _parse_response(elem)
            href_path = _urlparse.urlsplit(href).path.rstrip("/")
            if not href_path or href_path == self_path:
                continue  # the "." entry describing self, per RFC 4918
            name = href_path.rsplit("/", 1)[-1]
            if name:
                yield name, FileStat(
                    st_size=size, st_mtime=_utils.parsedate(lm), is_dir=is_dir
                )

    def _listdir(self):
        for name, _stat_ in self._scandir():
            yield name

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
        # Same fix as HttpPath.rmdir() (http_verify_and_fix.md Phase 5):
        # a PROPFIND Depth:1 on a non-collection resource returns only the
        # "." entry describing itself, which _scandir() already filters
        # out -- so a *file* looks exactly like an empty directory to
        # _listdir() alone, and rmdir() on a file silently DELETEd it.
        if not self.is_dir():
            raise NotADirectoryError(self)
        for _ in self._listdir():
            raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
        self.unlink()

    def rm(
        self,
        /,
        recursive: bool = False,
        missing_ok: bool = False,
        ignore_error: "bool | _ty.Callable[[Exception, DavPath], bool]" = False,
    ):
        if not recursive:
            return super().rm(
                recursive=recursive, missing_ok=missing_ok, ignore_error=ignore_error
            )
        # WebDAV DELETE is recursive by spec (RFC 4918): one request here
        # replaces the base implementation's client-side stat+walk+unlink.
        try:
            resp = self.backend.request("DELETE", self._wire_uri())
            if resp.status_code == 404:
                if not missing_ok:
                    raise FileNotFoundError(self)
                return
            resp.raise_for_status()
        except Exception as error:
            onerror = (
                ignore_error
                if callable(ignore_error)
                else (lambda _e, _p: bool(ignore_error))
            )
            if not onerror(error, self):
                raise

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
