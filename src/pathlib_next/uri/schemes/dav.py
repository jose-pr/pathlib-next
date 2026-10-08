from __future__ import annotations

import errno as _errno
import os as _os
import typing as _ty
import urllib.parse as _urlparse
import xml.etree.ElementTree as _ET

from ... import utils as _utils
from ...path import _check_follow
from ...utils.stat import FileStat
from .. import Uri
from ..source import _compose_uri
from .http import (
    _EXCLUSIVE_CREATE,
    _IDENTITY_ENCODING,
    HttpPath,
    _UploadStream,
    _path_error,
    _response_reader,
    _server_size,
    _split_userinfo,
    _translate_http_errors,
)

_NS = {"D": "DAV:"}
_MULTISTATUS = "{DAV:}multistatus"

_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:resourcetype/>
    <D:getcontentlength/>
    <D:getlastmodified/>
  </D:prop>
</D:propfind>"""


def _found_props(elem) -> "list":
    """The `<D:prop>` elements of every successful `<D:propstat>` of a
    `<D:response>`. RFC 4918 groups properties into one propstat per status
    (200 for found, 404 for missing) in no fixed order, so reading only the
    first one could read the 404 group; a propstat with no status counts."""
    props = []
    for propstat in elem.findall("D:propstat", _NS):
        code = _status_code(propstat.findtext("D:status", namespaces=_NS))
        prop = propstat.find("D:prop", _NS)
        if prop is not None and (code is None or 200 <= code < 300):
            props.append(prop)
    return props


def _find_prop(props, name: str):
    for prop in props:
        found = prop.find(name, _NS)
        if found is not None:
            return found
    return None


def _parse_response(elem) -> "tuple[str, bool, int, str]":
    href = elem.findtext("D:href", namespaces=_NS) or ""
    props = _found_props(elem)
    resourcetype = _find_prop(props, "D:resourcetype")
    is_dir = (
        resourcetype is not None and resourcetype.find("D:collection", _NS) is not None
    )
    size_elem = _find_prop(props, "D:getcontentlength")
    # A missing, non-numeric or negative length is no size.
    size = _server_size(size_elem.text if size_elem is not None else None) or 0
    lm_elem = _find_prop(props, "D:getlastmodified")
    lm = lm_elem.text if lm_elem is not None else None
    # Still percent-encoded: decoding before `urlsplit()` would read a
    # literal "#" or "?" in a name as URL syntax.
    return href, is_dir, size, lm


def _decoded_segments(path: str) -> "list[str]":
    """The decoded segments of a percent-encoded URL path, a trailing slash
    ignored. A segment that was `%2F` stays one segment. Undecodable bytes
    are kept (`surrogateescape`) as the URI layer keeps them."""
    return [
        _urlparse.unquote(segment, errors="surrogateescape")
        for segment in path.rstrip("/").split("/")
    ]


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


class _DavWriteStream(_UploadStream):
    def __init__(self, path: "DavPath", exclusive: bool = False):
        super().__init__(path)
        # `exclusive` (mode "x"): see `HttpWriteStream`.
        self._exclusive = exclusive
        self._ready = True

    def _upload(self, data):
        # 409: an intermediate collection is missing (RFC 4918 9.7.1).
        statuses = {409: FileNotFoundError}
        extra = {}
        if self._exclusive:
            statuses[412] = FileExistsError
            extra = _EXCLUSIVE_CREATE
        path = self._path
        path._dav_request(
            path.backend.write_method, data=data, statuses=statuses, **extra
        )


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
                if isinstance(error, type):
                    # `(errno, strerror, filename)`, as pathlib raises it.
                    raise _path_error(error, self)
                raise error(self)
            if resp.status_code == 207 and method in ("DELETE", "MOVE"):
                _raise_for_multistatus(resp, self)
            resp.raise_for_status()
        return resp

    def _propfind(self, depth="0"):
        return self._propfind_reply(depth)[0]

    def _propfind_reply(self, depth: str) -> "tuple[_ET.Element, str]":
        """The `<D:multistatus>` element of a PROPFIND reply, and the URL
        that answered it (not this path's own after a redirect)."""
        resp = self._dav_request(
            "PROPFIND",
            headers={"Depth": depth, "Content-Type": "application/xml"},
            data=_PROPFIND_BODY,
        )
        try:
            root = _ET.fromstring(resp.content)
        except _ET.ParseError:
            root = None
        if root is None or root.tag != _MULTISTATUS:
            # A non-WebDAV endpoint or proxy answering 200 with HTML or some
            # other XML: an I/O error, so `exists()`/`is_dir()` report False
            # instead of leaking a SyntaxError subclass, and a login page is
            # not an empty directory.
            raise OSError(_errno.EIO, f"Invalid PROPFIND response for {self}")
        return root, getattr(resp, "url", None) or self._wire_uri()

    def stat(self, *, follow_symlinks=True, walk_up_last_modified=False):
        # `walk_up_last_modified` has nothing to do: the reply carries the
        # modification time.
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
        root, answered = self._propfind_reply("1")
        collection = _urlparse.urlsplit(answered)
        # Compared segment by segment, decoded: the wire path is
        # percent-encoded, so a directory named "my dir" otherwise listed
        # itself as a child.
        here = _decoded_segments(collection.path)
        host = (collection.hostname or "").lower()
        base = collection._replace(
            path=collection.path.rstrip("/") + "/", query="", fragment=""
        ).geturl()
        selves = []
        members = []
        replies = 0
        for elem in root.findall("D:response", _NS):
            replies += 1
            href, is_dir, size, lm = _parse_response(elem)
            if not href.strip():
                continue
            try:
                # Split first, decode after: a literal "#"/"?" in a name
                # arrives encoded and must stay part of the name. Resolved
                # against the collection, as RFC 4918 8.3 reads a reference.
                where = _urlparse.urlsplit(_urlparse.urljoin(base, href.strip()))
            except ValueError:
                continue
            if (where.hostname or "").lower() != host:
                continue
            segments = _decoded_segments(where.path)
            if segments == here:
                selves.append(is_dir)
            elif segments[:-1] == here:
                # A direct member only: a deeper one (a server that ignores
                # the Depth) or one elsewhere is not listed here.
                members.append((segments[-1], is_dir, size, lm))
        if replies and not (selves or members):
            raise OSError(
                _errno.EIO,
                f"The PROPFIND reply for {self} names neither the collection "
                "nor any member of it",
            )
        if selves and not any(selves):
            # The "." entry describing self, per RFC 4918. A Depth:1
            # PROPFIND on a non-collection answers with only this entry,
            # which read as an empty directory.
            raise NotADirectoryError(
                _errno.ENOTDIR, _os.strerror(_errno.ENOTDIR), str(self)
            )
        for name, is_dir, size, lm in members:
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
        if mode == "r":
            req = self._get(self._wire_uri(), headers=_IDENTITY_ENCODING)
            content_type = req.headers.get("Content-Type", "")
            if content_type.split(";")[0].strip().lower() == "text/html":
                # GET on a collection is server-defined (RFC 4918 9.4) and
                # usually an HTML index, which was read back as content.
                # Only an HTML answer pays the PROPFIND that tells them apart.
                try:
                    is_dir = self.stat().is_dir()
                except OSError:
                    is_dir = False
                if is_dir:
                    req.close()
                    raise IsADirectoryError(
                        _errno.EISDIR, _os.strerror(_errno.EISDIR), str(self)
                    )
            return _response_reader(self, req, buffering)
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x":
            self._ensure_missing()
        return _DavWriteStream(self, exclusive=mode == "x")

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
        # stat(), not is_dir(): a missing path is FileNotFoundError, not
        # NotADirectoryError.
        if not self.stat().is_dir():
            raise NotADirectoryError(
                _errno.ENOTDIR, _os.strerror(_errno.ENOTDIR), str(self)
            )
        for _ in self._listdir():
            raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
        self._delete()

    def rm(
        self,
        /,
        recursive: bool = False,
        missing_ok: bool = False,
        ignore_error: "bool | _ty.Callable[[Exception, DavPath], bool]" = False,
        *,
        follow_symlinks=False,
        follow_binds=False,
    ):
        # A collection holds no symlinks or bindings: the policies are
        # checked and have nothing to decide.
        _check_follow("follow_symlinks", follow_symlinks)
        _check_follow("follow_binds", follow_binds)
        if not recursive:
            return super().rm(
                recursive=recursive,
                missing_ok=missing_ok,
                ignore_error=ignore_error,
                follow_symlinks=follow_symlinks,
                follow_binds=follow_binds,
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
        # The new path carries the target's own query, not this path's: a
        # signature or token in the query is good for the source only.
        moved = self._from_parsed_parts(
            self.source, target.path, target.query, target.fragment
        )
        # No userinfo in the header: the credentials already travel as
        # `auth=` (see `HttpBackend.request`), and a header value ends up in
        # server and proxy logs. No fragment either: it is not part of a URL
        # the server resolves.
        dest = _split_userinfo(moved.with_fragment("")._wire_uri())[0]
        self._dav_request(
            "MOVE",
            headers={"Destination": dest, "Overwrite": "F"},
            statuses={
                # 409: the destination's parent collection is missing.
                409: lambda _self: _path_error(FileNotFoundError, target),
                412: lambda _self: _path_error(FileExistsError, target),
            },
        )
        # pathlib returns the new path.
        return moved
