from __future__ import annotations

import errno as _errno
import io as _io
import os as _os
import typing as _ty
import urllib.parse as _urlparse
import xml.etree.ElementTree as _ET

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import Uri
from ..source import _compose_uri
from .http import (
    _IDENTITY_ENCODING,
    HttpPath,
    _response_reader,
    _split_userinfo,
    _translate_http_errors,
)

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
        resourcetype is not None and resourcetype.find("D:collection", _NS) is not None
    )
    size_text = (
        prop.findtext("D:getcontentlength", namespaces=_NS)
        if prop is not None
        else None
    )
    size = int(size_text) if size_text else 0
    lm = (
        prop.findtext("D:getlastmodified", namespaces=_NS) if prop is not None else None
    )
    # Still percent-encoded: decoding before `urlsplit()` would read a
    # literal "#" or "?" in a name as URL syntax.
    return href, is_dir, size, lm


def _status_code(text: "str | None") -> "int | None":
    # "HTTP/1.1 423 Locked"
    parts = (text or "").split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def _multistatus_failures(content: bytes) -> "list[tuple[str, int]] | None":
    """`(href, status)` of every failed member of a 207 reply, or `None`
    when the body is not a parseable multistatus."""
    try:
        root = _ET.fromstring(content)
    except _ET.ParseError:
        return None
    failures = []
    for response in root.findall("D:response", _NS):
        href = _urlparse.unquote(response.findtext("D:href", namespaces=_NS) or "")
        statuses = [response.findtext("D:status", namespaces=_NS)] + [
            propstat.findtext("D:status", namespaces=_NS)
            for propstat in response.findall("D:propstat", _NS)
        ]
        for text in statuses:
            code = _status_code(text)
            if code is not None and code >= 400:
                failures.append((href, code))
                break
    return failures


def _raise_for_multistatus(resp, path) -> None:
    """RFC 4918 9.6.1/9.9.4: DELETE and MOVE answer 207 when a member of
    the collection could not be processed -- a partial failure, not a
    success."""
    failures = _multistatus_failures(resp.content)
    if failures == []:
        return
    detail = (
        ", ".join(f"{href} (HTTP {code})" for href, code in failures)
        if failures
        else "unparseable 207 Multi-Status reply"
    )
    message = f"Partial failure for {path}: {detail}"
    if failures and all(code in (401, 403, 423) for _href, code in failures):
        raise PermissionError(_errno.EACCES, message)
    raise OSError(_errno.EIO, message)


class _DavWriteStream(_io.BytesIO):
    def __init__(self, path: "DavPath"):
        super().__init__()
        self._path = path

    def close(self):
        if self.closed:
            return
        try:
            # 409: an intermediate collection is missing (RFC 4918 9.7.1).
            self._path._dav_request(
                "PUT", data=self.getvalue(), statuses={409: FileNotFoundError}
            )
        finally:
            # Mark closed even on a failed upload, as `HttpWriteStream`
            # does: otherwise `IOBase.__del__` sends the PUT again at GC.
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
        # Keeps the userinfo: `HttpBackend.request` strips it from the URL
        # it sends and turns it into `auth=`.
        # Direct string assembly instead of
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

    def _dav_request(self, method, *, statuses=None, **kwargs):
        """Send `method` to this resource with pathlib exception types:
        `statuses` maps a status code to the exception class raised for it
        (called with `self`), ahead of `_translate_http_errors`' generic
        mapping; a 207 to DELETE/MOVE raises for its failed members; 423
        Locked is a PermissionError."""
        statuses = {
            404: FileNotFoundError,
            401: PermissionError,
            403: PermissionError,
            423: PermissionError,
            **(statuses or {}),
        }
        with _translate_http_errors(self):
            resp = self.backend.request(method, self._wire_uri(), **kwargs)
            error = statuses.get(resp.status_code)
            if error is not None:
                resp.close()
                raise error(self)
            if resp.status_code == 207 and method in ("DELETE", "MOVE"):
                _raise_for_multistatus(resp, self)
            resp.raise_for_status()
        return resp

    def _propfind(self, depth="0"):
        resp = self._dav_request(
            "PROPFIND",
            headers={"Depth": depth, "Content-Type": "application/xml"},
            data=_PROPFIND_BODY,
        )
        try:
            return _ET.fromstring(resp.content)
        except _ET.ParseError:
            # A non-WebDAV endpoint or proxy answering 200 with HTML: an
            # I/O error, so `exists()`/`is_dir()` report False instead of
            # leaking a SyntaxError subclass.
            raise OSError(_errno.EIO, f"Invalid PROPFIND response for {self}") from None

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
        # Both sides compared decoded: the wire path is percent-encoded, so
        # a directory named "my dir" otherwise listed itself as a child.
        self_path = _urlparse.unquote(_urlparse.urlsplit(self._wire_uri()).path).rstrip(
            "/"
        )
        for elem in root.findall("D:response", _NS):
            href, is_dir, size, lm = _parse_response(elem)
            # Split first, decode after: a literal "#"/"?" in a name arrives
            # encoded and must stay part of the name.
            href_path = _urlparse.unquote(_urlparse.urlsplit(href).path).rstrip("/")
            if not href_path or href_path == self_path:
                continue  # the "." entry describing self, per RFC 4918
            name = href_path.rsplit("/", 1)[-1]
            # The href is untrusted: an entry such as `%2E%2E/` decodes to
            # "..", which let a recursive copy write outside its destination.
            if _utils.is_safe_child_name(name):
                yield name, FileStat(
                    st_size=size, st_mtime=_utils.parsedate(lm), is_dir=is_dir
                )

    def _listdir(self):
        for name, _stat_ in self._scandir():
            yield name

    def _open(self, mode="r", buffering=-1):
        if "r" in mode:
            with _translate_http_errors(self):
                req = self.backend.request(
                    "GET", self._wire_uri(), stream=True, headers=_IDENTITY_ENCODING
                )
                try:
                    req.raise_for_status()
                except BaseException:
                    # Without this check a 404/401/500 error page was read
                    # back as file content. Close first: stream=True leaves
                    # the body unread and the pooled connection held.
                    req.close()
                    raise
            return _response_reader(self, req, buffering)
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _DavWriteStream(self)

    def _mkdir(self, mode):
        self._dav_request(
            "MKCOL",
            statuses={
                409: FileNotFoundError,
                405: FileExistsError,
                501: FileExistsError,
            },
        )

    def unlink(self, missing_ok=False):
        # WebDAV DELETE on a collection is recursive (RFC 4918), so a bare
        # DELETE here removed a whole tree. pathlib's unlink() never removes
        # a directory: check with a Depth:0 PROPFIND first. The recursive
        # DELETE stays available through `rm(recursive=True)`.
        try:
            root = self._propfind(depth="0")
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        responses = root.findall("D:response", _NS)
        if responses and _parse_response(responses[0])[1]:
            raise IsADirectoryError(
                _errno.EISDIR, _os.strerror(_errno.EISDIR), str(self)
            )
        self._delete(missing_ok=missing_ok)

    def _delete(self, missing_ok=False):
        # The raw DELETE, with no collection guard: `rmdir()` has already
        # verified an empty directory before calling it.
        try:
            self._dav_request("DELETE")
        except FileNotFoundError:
            if not missing_ok:
                raise

    def rmdir(self):
        # Same fix as HttpPath.rmdir():
        # a PROPFIND Depth:1 on a non-collection resource returns only the
        # "." entry describing itself, which _scandir() already filters
        # out -- so a *file* looks exactly like an empty directory to
        # _listdir() alone, and rmdir() on a file silently DELETEd it.
        if not self.is_dir():
            raise NotADirectoryError(self)
        for _ in self._listdir():
            raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
        self._delete()

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
            try:
                self._dav_request("DELETE")
            except FileNotFoundError:
                if not missing_ok:
                    raise
        except Exception as error:
            onerror = (
                ignore_error
                if callable(ignore_error)
                else (lambda _e, _p: bool(ignore_error))
            )
            if not onerror(error, self):
                raise

    def rename(self, target: "DavPath | Uri | str"):
        target = self._rename_target(target)
        # No userinfo in the header: the credentials already travel as
        # `auth=` (see `HttpBackend.request`), and a header value ends up in
        # server and proxy logs.
        dest = _split_userinfo(self.with_path(target.path)._wire_uri())[0]
        self._dav_request(
            "MOVE",
            headers={"Destination": dest, "Overwrite": "F"},
            statuses={
                # 409: the destination's parent collection is missing.
                409: lambda _self: FileNotFoundError(target),
                412: lambda _self: FileExistsError(target),
            },
        )
