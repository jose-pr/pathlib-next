from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import html.parser as _html_parser
import io as _io
import os as _os
import re as _re
import time as _time
import typing as _ty
import urllib.parse as _urlparse

import requests as _req
import urllib3.exceptions as _urllib3_exc

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import UriPath

DEFAULT_TIMEOUT = (10, 60)
"""`(connect, read)` timeout, in seconds, `HttpBackend` sends with every
request unless the caller supplies `timeout` (via `with_session(...,
timeout=...)` / `requests_args`, or per request); `timeout=None` there
restores requests' unbounded wait."""

_RE_URL_SCHEME = _re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")

_IDENTITY_ENCODING = {"Accept-Encoding": "identity"}
"""Sent on content GETs and `stat()` probes: requests' session advertises
gzip/deflate, and a compressing server's reply would otherwise come back
compressed from `open()` (and a rewrite-mode append would store that blob),
while `Content-Length` would count encoded bytes."""


def _split_userinfo(url: str) -> "tuple[str, tuple[str, str] | None]":
    """Split the userinfo out of an absolute URL: `(url_without_userinfo,
    (user, password) | None)`, both parts percent-decoded. Credentials sent
    inside the request URL end up in `Response.url`, `raise_for_status()`
    text and redirect handling; they go out as `auth=` instead."""
    match = _RE_URL_SCHEME.match(url)
    if not match:
        return url, None
    start = match.end()
    end = len(url)
    for sep in "/?#":
        index = url.find(sep, start)
        if index != -1 and index < end:
            end = index
    userinfo, at, hostport = url[start:end].rpartition("@")
    if not at:
        return url, None
    user, _, password = userinfo.partition(":")
    auth = (_urlparse.unquote(user), _urlparse.unquote(password))
    return url[:start] + hostport + url[end:], (auth if any(auth) else None)


_ERRNOS = {
    FileNotFoundError: _errno.ENOENT,
    PermissionError: _errno.EACCES,
    FileExistsError: _errno.EEXIST,
    IsADirectoryError: _errno.EISDIR,
    NotADirectoryError: _errno.ENOTDIR,
}


def _path_error(error_cls, path_obj) -> OSError:
    """`error_cls` built as pathlib builds it -- `(errno, strerror,
    filename)` -- so `e.errno` and `e.filename` are set."""
    code = _ERRNOS.get(error_cls, _errno.EIO)
    return error_cls(code, _os.strerror(code), str(path_obj))


@_contextlib.contextmanager
def _translate_http_errors(path_obj, *, conflict=FileExistsError):
    """Map a failed request to a pathlib exception for `path_obj`.
    `conflict` is what a 409 means: for a write (PUT, MKCOL, MOVE) RFC 4918
    uses 409 for a missing parent collection, so writers pass
    FileNotFoundError."""
    # `from None`: a requests exception can carry a URL (a proxy URL from
    # the environment included) with credentials in it, and a chained cause
    # is printed by every formatted traceback and `logging.exception`. The
    # status code/reason or the exception type stay in the message instead.
    try:
        yield
    except _req.exceptions.HTTPError as e:
        response = e.response
        status = response.status_code if response is not None else None
        reason = getattr(response, "reason", None) or ""
        if status in (404, 410):
            raise _path_error(FileNotFoundError, path_obj) from None
        elif status in (401, 403):
            raise _path_error(PermissionError, path_obj) from None
        elif status == 409:
            raise _path_error(conflict, path_obj) from None
        elif status in (405, 501):
            raise PermissionError(
                _errno.EACCES,
                f"Method not allowed (HTTP {status})",
                str(path_obj),
            ) from None
        else:
            raise OSError(
                _errno.EIO,
                f"HTTP Error {status} {reason}".rstrip() + f" for {path_obj}",
            ) from None
    except _req.exceptions.Timeout as e:
        raise TimeoutError(f"Timeout for {path_obj} ({type(e).__name__})") from None
    except _req.exceptions.ConnectionError as e:
        raise ConnectionError(
            f"Connection error for {path_obj} ({type(e).__name__})"
        ) from None
    except _req.exceptions.RequestException as e:
        raise OSError(
            _errno.EIO, f"Request failed for {path_obj} ({type(e).__name__})"
        ) from None


_RE_ISO8601 = _re.compile(r"\d{4}-\d+-\d+T\d+:\d{2}:\d{2}Z")
_DATETIME_FMTs = (
    (_re.compile(r"\d+-[A-S][a-y]{2}-\d{4} \d+:\d{2}:\d{2}"), "%d-%b-%Y %H:%M:%S"),
    (_re.compile(r"\d+-[A-S][a-y]{2}-\d{4} \d+:\d{2}"), "%d-%b-%Y %H:%M"),
    (_re.compile(r"\d{4}-\d+-\d+ \d+:\d{2}:\d{2}"), "%Y-%m-%d %H:%M:%S"),
    (_RE_ISO8601, "%Y-%m-%dT%H:%M:%SZ"),
    (_re.compile(r"\d{4}-\d+-\d+ \d+:\d{2}"), "%Y-%m-%d %H:%M"),
    (_re.compile(r"\d{4}-[A-S][a-y]{2}-\d+ \d+:\d{2}:\d{2}"), "%Y-%b-%d %H:%M:%S"),
    (_re.compile(r"\d{4}-[A-S][a-y]{2}-\d+ \d+:\d{2}"), "%Y-%b-%d %H:%M"),
    (
        _re.compile(r"[F-W][a-u]{2} [A-S][a-y]{2} +\d+ \d{2}:\d{2}:\d{2} \d{4}"),
        "%a %b %d %H:%M:%S %Y",
    ),
    (
        _re.compile(r"[F-W][a-u]{2}, \d+ [A-S][a-y]{2} \d{4} \d{2}:\d{2}:\d{2} \S+"),
        "%a, %d %b %Y %H:%M:%S %Z",
    ),
    (_re.compile(r"\d{4}-\d+-\d+"), "%Y-%m-%d"),
    (_re.compile(r"\d+/\d+/\d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}"), "%d/%m/%Y %H:%M:%S %z"),
    (_re.compile(r"\d{2} [A-S][a-y]{2} \d{4}"), "%d %b %Y"),
)

_RE_FILESIZE = _re.compile(r"\d[\d,]*(\.\d+)? ?[BKMGTPEZY]|\d[\d,]*|-", _re.I)
_RE_COMMONHEAD = _re.compile(
    "Name|(Last )?modifi(ed|cation)|date|Size|Description|Metadata|Type|Parent Directory",
    _re.I,
)
_RE_HEAD_NAME = _re.compile("name$|^file|^download")
_RE_HEAD_MOD = _re.compile("modifi|^uploaded|date|time")
_RE_HEAD_SIZE = _re.compile("size|bytes$")


def _human2bytes(s):
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        symbols = "BKMGTPEZY"
        letter = s[-1:].strip().upper()
        num = float(s[:-1])
        prefix = {symbols[0]: 1}
        for i, sym in enumerate(symbols[1:]):
            prefix[sym] = 1 << (i + 1) * 10
        return int(num * prefix.get(letter, 1))


def _aherf2filename(a_href):
    isdir = "/" if a_href.endswith("/") else ""
    path = _urlparse.urlsplit(a_href).path
    return _urlparse.unquote(path.rstrip("/")).rsplit("/", 1)[-1] + isdir


_DEFAULT_PORTS = {"http": 80, "https": 443}

# A listing is parsed only from an HTML reply (or one with no Content-Type).
_LISTING_TYPES = ("text/html", "application/xhtml+xml")


def _origin(split: _urlparse.SplitResult) -> "tuple[str, str, int | None]":
    scheme = split.scheme.lower()
    try:
        port = split.port
    except ValueError:
        port = -1
    return scheme, (split.hostname or ""), port or _DEFAULT_PORTS.get(scheme)


class _DirectoryListingParser(_html_parser.HTMLParser):
    """Scrapes an HTML directory index into `_FileEntry`s.

    `base_url` is the URL the listing was fetched from (after redirects).
    When given, an entry is kept only if its href, resolved against that URL
    as a browser resolves it, names a direct child of the listed directory
    on the same origin -- so a page's links elsewhere, and a reverse proxy
    whose `<title>` names a different path than the request, are handled.
    Without it, absolute hrefs are scoped by the "Index of ..." title."""

    def __init__(self, base_url: "str | None" = None):
        super().__init__()
        self.listing = []
        self.base_url = base_url
        self._base_origin = None
        self._base_segments = None
        if base_url:
            split = _urlparse.urlsplit(base_url)
            self._base_origin = _origin(split)
            self._base_segments = [
                _urlparse.unquote(segment)
                for segment in split.path.rstrip("/").split("/")
            ]

        self.in_title = False
        self.in_pre = False
        self.in_table = False
        self.in_tr = False
        self.in_td = False
        self.in_a = False

        self.title_text = ""
        self.cwd = None

        self.table_rows = []
        self.current_row = []
        self.current_cell_text = []
        self.current_cell_href = None
        self.headers = None

        self.last_href = None
        self.pre_collect_data = False
        self.pre_data_buffer = []

        self.all_links = []
        self.current_a_href = None
        self.current_a_text = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "title":
            self.in_title = True
            self.title_text = ""
        elif tag == "pre":
            self.in_pre = True
        elif tag == "table":
            self.in_table = True
            self.table_rows = []
            self.headers = None
        elif tag == "tr" and self.in_table:
            self.in_tr = True
            self.current_row = []
        elif (tag == "td" or tag == "th") and self.in_tr:
            self.in_td = True
            self.current_cell_text = []
            self.current_cell_href = None
        elif tag == "a":
            self.in_a = True
            href = attrs_dict.get("href")
            if href:
                self.current_a_href = href
                self.current_a_text = []
                if self.in_td:
                    self.current_cell_href = href
                elif self.in_pre:
                    self._flush_pre_entry()
                    self.last_href = href
                    self.pre_collect_data = False

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
            title = self.title_text.strip()
            if title.startswith("Index of "):
                self.cwd = title[9:]
        elif tag == "pre":
            self._flush_pre_entry()
            self.in_pre = False
        elif tag == "table":
            self.in_table = False
            self._process_table()
        elif tag == "tr" and self.in_tr:
            self.in_tr = False
            self.table_rows.append(self.current_row)
        elif (tag == "td" or tag == "th") and self.in_td:
            self.in_td = False
            cell_text = "".join(self.current_cell_text).strip()
            self.current_row.append((cell_text, self.current_cell_href))
        elif tag == "a":
            self.in_a = False
            if self.current_a_href:
                text = "".join(self.current_a_text).strip()
                self.all_links.append((text, self.current_a_href))
                self.current_a_href = None
            if self.in_pre and self.last_href:
                self.pre_data_buffer = []
                self.pre_collect_data = True

    def handle_data(self, data):
        if self.in_title:
            self.title_text += data
        elif self.in_td:
            self.current_cell_text.append(data)
        elif self.in_pre and self.pre_collect_data:
            self.pre_data_buffer.append(data)
        if self.in_a:
            self.current_a_text.append(data)

    def _is_child_href(self, href) -> bool:
        """Whether `href`, resolved against `base_url`, is a direct child of
        the listed directory on the same scheme, host and port."""
        split = _urlparse.urlsplit(_urlparse.urljoin(self.base_url, href))
        if _origin(split) != self._base_origin:
            return False
        segments = [
            _urlparse.unquote(segment) for segment in split.path.rstrip("/").split("/")
        ]
        base = self._base_segments
        return (
            len(segments) == len(base) + 1
            and segments[:-1] == base
            and segments[-1] != ""
        )

    def _is_ancestor_href(self, href):
        # With a `base_url`, anything but a direct child is skipped.
        # An absolute href is normally the "up a level" link -- Apache/
        # nginx don't always render it as "../" (e.g. "/files/" from
        # "/files/sub/"), and `_aherf2filename()` only looks at the href's
        # last path segment, not the anchor's text, so that case isn't
        # caught by the Parent Directory/../ name check above. But some
        # reverse-proxied or absolute-URL-configured servers render EVERY
        # entry as an absolute href, not just the parent link -- a blanket
        # `startswith('/')` filter would then silently drop the whole
        # listing. Scope the filter to hrefs outside the current listing's
        # own path instead (falls back to the old blanket behavior if the
        # listing had no parseable "Index of ..." <title>).
        if self.base_url:
            return not self._is_child_href(href)
        if not href.startswith("/"):
            return False
        if not self.cwd:
            return True
        path = _urlparse.unquote(_urlparse.urlsplit(href).path)
        cwd = self.cwd if self.cwd.endswith("/") else self.cwd + "/"
        # a strict descendant of cwd is a real child; cwd itself (a
        # self-referencing "up" link, e.g. at the site root) or anything
        # outside cwd is the ancestor/parent link.
        return path == cwd or not path.startswith(cwd)

    def _flush_pre_entry(self):
        if not self.last_href:
            return

        name = _aherf2filename(self.last_href)
        if (
            name in ("Parent Directory", "..", "../")
            or self.last_href.startswith("?")
            or self._is_ancestor_href(self.last_href)
        ):
            self.last_href = None
            return

        modified = None
        size = None
        size_exact = True
        description = None

        text = (
            "".join(self.pre_data_buffer).replace("\r", "").split("\n", 1)[0].lstrip()
        )
        if text:
            for regex, fmt in _DATETIME_FMTs:
                match = regex.match(text)
                if match:
                    try:
                        modified = _time.strptime(match.group(0), fmt)
                    except ValueError:
                        pass
                    text = text[match.end() :].lstrip()
                    break

            match = _RE_FILESIZE.match(text)
            if match:
                sizestr = match.group(0)
                if sizestr != "-":
                    size = _human2bytes(sizestr.replace(" ", "").replace(",", ""))
                    size_exact = _is_exact_size(sizestr)
                text = text[match.end() :].lstrip()

            if text:
                description = text.rstrip()
                if description == "/":
                    name += "/"
                    description = None

        self.listing.append(_FileEntry(name, modified, size, description, size_exact))
        self.last_href = None

    def _process_table(self):
        started = False
        for row in self.table_rows:
            has_head = False
            cell_texts = [cell[0] for cell in row]
            for text in cell_texts:
                if _RE_COMMONHEAD.search(text):
                    has_head = True
                    break

            if has_head and not started:
                self.headers = []
                name_found = False
                for text in cell_texts:
                    norm = text.strip(" \t\n\r\x0b\x0c\xa0↑↓").lower()
                    if not norm:
                        continue
                    if not name_found and _RE_HEAD_NAME.search(norm):
                        self.headers.append("name")
                        name_found = True
                    elif norm in ("size", "description"):
                        self.headers.append(norm)
                    elif _RE_HEAD_MOD.search(norm):
                        self.headers.append("modified")
                    elif _RE_HEAD_SIZE.search(norm):
                        self.headers.append("size")
                    elif norm.endswith("signature"):
                        self.headers.append("signature")
                    else:
                        self.headers.append("description")
                if not self.headers:
                    self.headers = ["name", "modified", "size", "description"]
                elif not name_found:
                    self.headers[0] = "name"
                started = True
                continue

            if started:
                file_name = None
                file_mod = None
                file_size = None
                file_size_exact = True
                file_desc = None

                status = 0
                for cell_text, cell_href in row:
                    if status >= len(self.headers):
                        break

                    header = self.headers[status]
                    if header == "name":
                        if not cell_href or cell_href.startswith("#"):
                            continue
                        name_val = cell_text.strip()
                        if name_val == "Parent Directory" or cell_href == "../":
                            break
                        if self.base_url and not self._is_child_href(cell_href):
                            break
                        file_name = _aherf2filename(cell_href)
                        status = 1
                    elif header == "modified":
                        timestr = cell_text.strip()
                        if timestr:
                            for regex, fmt in _DATETIME_FMTs:
                                match = regex.match(timestr)
                                if match:
                                    try:
                                        file_mod = _time.strptime(match.group(0), fmt)
                                    except ValueError:
                                        pass
                                    break
                        status += 1
                    elif header == "size":
                        sizestr = cell_text.strip().replace(",", "")
                        if sizestr and sizestr != "-":
                            match = _RE_FILESIZE.match(sizestr)
                            if match:
                                file_size = _human2bytes(
                                    match.group(0).replace(" ", "")
                                )
                                file_size_exact = _is_exact_size(match.group(0))
                        status += 1
                    elif header == "description":
                        file_desc = cell_text or None
                        status += 1
                    else:
                        status += 1

                if file_name:
                    self.listing.append(
                        _FileEntry(
                            file_name, file_mod, file_size, file_desc, file_size_exact
                        )
                    )

    def close(self):
        super().close()
        self._flush_pre_entry()
        if not self.listing:
            for text, href in self.all_links:
                name = _aherf2filename(href)
                if (
                    name in ("Parent Directory", "..", "../")
                    or href.startswith("?")
                    or self._is_ancestor_href(href)
                ):
                    continue
                self.listing.append(_FileEntry(name, None, None, None))


class _FileEntry(_ty.NamedTuple):
    name: str
    modified: _ty.Optional[_time.struct_time]
    size: _ty.Optional[int]
    description: _ty.Optional[str]
    # False when `size` came from a human-readable column ("1.2K"): the
    # value is approximate and must not be reported as `st_size`.
    size_exact: bool = True


def _is_exact_size(sizestr: str) -> bool:
    sizestr = sizestr.replace(" ", "").replace(",", "")
    if sizestr[-1:] in ("b", "B"):
        sizestr = sizestr[:-1]
    return sizestr.isdigit()


class _ResponseReader(_io.RawIOBase):
    """Raw read stream over a streamed `requests` response body.

    Reads go through `iter_content()`, so a body the server encoded anyway
    (despite `Accept-Encoding: identity`) is decoded, and a failure in the
    middle of the body surfaces as `TimeoutError`/`ConnectionError`/
    `OSError` rather than a urllib3 exception. A decoded chunk larger than
    the caller's buffer is kept for the next `readinto()`."""

    def __init__(self, path, response: _req.Response, chunk_size: int):
        super().__init__()
        self._path = path
        self._response = response
        self._chunks = response.iter_content(chunk_size)
        self._pending = b""
        self._offset = 0

    def readable(self):
        return True

    def _next_chunk(self) -> bytes:
        path = self._path
        try:
            return next(self._chunks, b"")
        except _req.exceptions.RequestException as error:
            # requests reports a read timeout inside the body as its
            # ConnectionError: keep it a TimeoutError, like a request that
            # stalls before its headers.
            if any(isinstance(a, _urllib3_exc.ReadTimeoutError) for a in error.args):
                raise TimeoutError(f"Timeout for {path} (ReadTimeoutError)") from None
            with _translate_http_errors(path):
                raise
        except _urllib3_exc.HTTPError as error:
            raise OSError(
                _errno.EIO, f"Read failed for {path} ({type(error).__name__})"
            ) from None

    def _check_complete(self) -> None:
        # urllib3 1.26 ends a streamed body silently when the connection
        # closes early; 2.x raises. A short body must never read as a
        # complete file.
        length = self._response.headers.get("Content-Length", "")
        tell = getattr(self._response.raw, "tell", None)
        if length.isdigit() and callable(tell) and tell() < int(length):
            raise OSError(
                _errno.EIO,
                f"Incomplete read for {self._path}: {tell()} of {length} bytes",
            )

    def readinto(self, buffer):
        view = memoryview(buffer).cast("B")
        if not view:
            return 0
        while self._offset >= len(self._pending):
            chunk = self._next_chunk()
            if not chunk:
                self._check_complete()
                return 0
            self._pending, self._offset = chunk, 0
        count = min(len(view), len(self._pending) - self._offset)
        view[:count] = self._pending[self._offset : self._offset + count]
        self._offset += count
        return count

    def close(self):
        if not self.closed:
            try:
                self._response.close()
            finally:
                super().close()


def _response_reader(path, response: _req.Response, buffering: int):
    buffer_size = _io.DEFAULT_BUFFER_SIZE if buffering < 0 else buffering
    raw = _ResponseReader(path, response, buffer_size or _io.DEFAULT_BUFFER_SIZE)
    return raw if buffer_size == 0 else _io.BufferedReader(raw, buffer_size)


class HttpWriteStream(_io.BytesIO):
    def __init__(self, path: "HttpPath"):
        super().__init__()
        self._path = path

    def close(self):
        if self.closed:
            return
        data = self.getvalue()
        try:
            with _translate_http_errors(self._path, conflict=FileNotFoundError):
                resp = self._path.backend.request(
                    self._path.backend.write_method,
                    self._path.as_uri(),
                    data=data,
                )
                resp.raise_for_status()
        finally:
            # Mark closed even on a failed upload -- otherwise a later
            # close() (context-manager __exit__ cleanup, or GC via
            # IOBase.__del__) silently retries the PUT.
            super().close()


class HttpAppendStream(_io.BytesIO):
    def __init__(self, path: "HttpPath"):
        super().__init__()
        self._path = path
        if path.backend.append_mode == "rewrite":
            try:
                existing = path.read_bytes()
            except FileNotFoundError:
                existing = b""
            self.write(existing)
        else:
            # "patch" mode: stat the resource to get its size as start offset.
            # Drop a listing-derived hint first: an offset taken from a hint
            # would overwrite the file wherever the listing was wrong.
            path._pop_stat_hint()
            try:
                stat = path.stat()
                self._start_offset = stat.st_size
                self._existed = True
            except FileNotFoundError:
                self._start_offset = 0
                self._existed = False

    def close(self):
        if self.closed:
            return
        try:
            if self._path.backend.append_mode == "rewrite":
                with _translate_http_errors(self._path, conflict=FileNotFoundError):
                    data = self.getvalue()
                    resp = self._path.backend.request(
                        self._path.backend.write_method,
                        self._path.as_uri(),
                        data=data,
                    )
                    resp.raise_for_status()
            else:
                # "patch" mode: send only new content via Content-Range
                with _translate_http_errors(self._path, conflict=FileNotFoundError):
                    new_data = self.getvalue()
                    if not new_data:
                        # `bytes N-(N-1)/*` is not a valid range: there is
                        # nothing to append, only a missing file to create.
                        if not self._existed:
                            resp = self._path.backend.request(
                                self._path.backend.write_method,
                                self._path.as_uri(),
                                data=b"",
                            )
                            resp.raise_for_status()
                        return
                    start = self._start_offset
                    end = start + len(new_data) - 1
                    headers = {"Content-Range": f"bytes {start}-{end}/*"}
                    resp = self._path.backend.request(
                        "PATCH",
                        self._path.as_uri(),
                        data=new_data,
                        headers=headers,
                    )
                    resp.raise_for_status()
        finally:
            super().close()


class HttpBackend(_ty.NamedTuple):
    """Per-instance `requests.Session` + extra request kwargs shared by an
    `HttpPath` tree (see `with_session()`).

    Every request gets `timeout=DEFAULT_TIMEOUT` (`(10, 60)` seconds)
    unless `requests_args` or the call supplies `timeout` (`None` there
    waits forever). URL userinfo (`http://user:password@host/`) is never
    sent inside the request URL: it is stripped and sent as `auth=(user,
    password)` -- unless `requests_args`/the call pass their own `auth`
    or the session has `session.auth` set, which win as they did before."""

    session: _req.Session
    requests_args: dict
    write_method: str = "PUT"
    append_mode: str = "rewrite"

    def request(self, method, uri: "HttpPath|str", **kwargs):
        url, auth = _split_userinfo(uri if isinstance(uri, str) else uri.as_uri(False))
        args = {**self.requests_args, **kwargs}
        if self.requests_args.get("headers") and kwargs.get("headers"):
            # Merged key by key: the caller's `with_session(headers=...)`
            # (an auth token, say) must survive a request that sends its
            # own headers (PROPFIND `Depth`, MOVE `Destination`); the
            # request's own values win on a shared key.
            args["headers"] = {**self.requests_args["headers"], **kwargs["headers"]}
        args.setdefault("timeout", DEFAULT_TIMEOUT)
        if (
            auth is not None
            and "auth" not in args
            and not getattr(self.session, "auth", None)
        ):
            args["auth"] = auth
        return self.session.request(method=method, url=url, **args)


class HttpPath(UriPath):
    """`http`/`https` scheme: read/write access over HTTP (`PUT`/`DELETE`
    for writes/deletes, configurable via `with_session()`), listing
    directories by scraping an Apache/nginx-style HTML index with a
    zero-dependency in-house parser (`_DirectoryListingParser`). Requires
    the `http` extra."""

    __SCHEMES = ("http", "https")
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: HttpBackend

    def _initbackend(self):
        return HttpBackend(_req.Session(), {})

    def _listdir(self) -> list[_FileEntry]:
        # requests follows GET redirects by default, so a redirecting
        # server (e.g. Apache/nginx 301-ing "/sub" -> "/sub/") already
        # works with a single request. This retry only helps a
        # non-redirecting server/proxy that 404s the slash-less path.
        try:
            req = self._get_listing(self)
        except FileNotFoundError:
            if self.path.endswith("/"):
                raise
            req = self._get_listing(self.with_path(self.path + "/"))
        try:
            content_type = req.headers.get("Content-Type") or ""
            mime = content_type.split(";", 1)[0].strip().lower()
            if mime and mime not in _LISTING_TYPES:
                # A file: pathlib's iterdir() raises, and its body -- maybe
                # gigabytes -- is never downloaded to look for links.
                raise _path_error(NotADirectoryError, self)
            text = req.text
        finally:
            req.close()
        # Scoped by the URL actually answered (after redirects), not by the
        # page's own <title>.
        parser = _DirectoryListingParser(base_url=getattr(req, "url", None))
        parser.feed(text)
        parser.close()
        return parser.listing

    def _get_listing(self, uri) -> _req.Response:
        with _translate_http_errors(self):
            req = self.backend.request("GET", uri, stream=True)
            try:
                req.raise_for_status()
            except BaseException:
                req.close()
                raise
        return req

    def _scandir(self):
        # `_listdir()`'s single GET already carries type/size/mtime for
        # every child -- reuse it instead of `iterdir()` + a HEAD per child.
        for entry in self._listdir():
            # Directory-listing entries for subdirectories conventionally
            # carry a trailing "/" (e.g. htmllistparse's FileEntry.name ==
            # "sub/"). Without stripping it, the child's own .path would end
            # in "/" too, and Pathname.name derives from segments[-1] --
            # which is "" for a trailing-slash path, so every subdirectory
            # entry silently got name == "".
            is_dir = entry.name.endswith("/")
            name = entry.name.removesuffix("/")
            if not _utils.is_safe_child_name(name):
                # A listing is untrusted input: wsgidav's table renders its
                # parent row as `<a href="..">`, and a `<pre>` index can
                # carry `./`. Yielding "." or ".." as a child made
                # `child.unlink()` DELETE the parent collection (the client
                # normalizes `/d/..` to `/`) and `walk()` loop forever.
                # Filtered here so every parser branch is covered.
                continue
            if is_dir:
                stat = FileStat(
                    st_size=0, st_mtime=_utils.parsedate(entry.modified), is_dir=True
                )
            elif entry.size is not None and entry.size_exact and entry.modified:
                stat = FileStat(
                    st_size=entry.size,
                    st_mtime=_utils.parsedate(entry.modified),
                    is_dir=False,
                )
            else:
                # No exact byte count or no date in the listing (the stdlib
                # `<ul>` index, a "1.2K" column): a hint would hand the
                # child's first `stat()` a made-up size/mtime.
                stat = None
            yield name, stat

    def _is_dir(self, resp: _req.Response):
        # Judged on the FINAL response, after redirects: a redirect itself
        # is no directory signal (http->https, CDN and "latest" download
        # links all redirect to files).
        return (
            resp.url.endswith("/")
            or resp.url.endswith("/..")
            or resp.url.endswith("/.")
        )

    def stat(self, *, follow_symlinks=True, walk_up_last_modified=False):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint

        with _translate_http_errors(self):
            # The caller's own spelling first: a trailing-slash path the
            # server answers is a directory, even when the slash-less URL
            # answers too (wsgidav serves both `/d` and `/d/` with 200).
            check = (
                [self, self.with_path(self.path.removesuffix("/"))]
                if self.path.endswith("/")
                else [self]
            )
            for uri in check:
                resp = self.backend.request(
                    "HEAD", uri, allow_redirects=False, headers=_IDENTITY_ENCODING
                )
                resp.close()
                if resp.status_code == 405:
                    # Some servers reject HEAD outright; fall back to GET.
                    resp = self.backend.request(
                        "GET",
                        uri,
                        allow_redirects=False,
                        stream=True,
                        headers=_IDENTITY_ENCODING,
                    )
                    resp.close()
                if resp.status_code < 400:
                    break

            if resp.is_redirect:
                resp = self.backend.request("HEAD", uri, headers=_IDENTITY_ENCODING)
                resp.close()
                if resp.status_code == 405:
                    # Mirror the pre-redirect loop's HEAD-405 fallback --
                    # without this, a server/proxy that rejects HEAD
                    # everywhere (not just pre-redirect) surfaced
                    # PermissionError for an existing, redirect-only path.
                    resp = self.backend.request(
                        "GET", uri, stream=True, headers=_IDENTITY_ENCODING
                    )
                    resp.close()
            resp.raise_for_status()
            # From the final URL, once any redirect has been followed.
            is_dir = self._is_dir(resp)

        st_size = 0 if is_dir else int(resp.headers.get("Content-Length", 0))
        lm = resp.headers.get("Last-Modified")
        if lm is None and walk_up_last_modified:
            parent = self.parent
            if self != parent:
                try:
                    entry = next(
                        filter(
                            lambda p: p.name.removesuffix("/") == self.name,
                            parent._listdir(),
                        )
                    )
                    if entry and entry.modified:
                        lm = entry.modified
                except (StopIteration, OSError):
                    pass

        return FileStat(st_size=st_size, st_mtime=_utils.parsedate(lm), is_dir=is_dir)

    def _open(
        self,
        mode="r",
        buffering=-1,
    ):
        if "r" in mode:
            with _translate_http_errors(self):
                req = self.backend.request(
                    "GET", self.as_uri(), stream=True, headers=_IDENTITY_ENCODING
                )
                try:
                    req.raise_for_status()
                except BaseException:
                    req.close()
                    raise
            return _response_reader(self, req, buffering)
        if mode == "a":
            return HttpAppendStream(self)
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return HttpWriteStream(self)

    def unlink(self, missing_ok=False):
        # A server that honours DELETE on a collection (WebDAV, RFC 4918)
        # removes the whole tree, while pathlib's unlink() never removes a
        # directory -- refuse one. Limitation: over http: this relies on the
        # HEAD-based `stat()` heuristic, which cannot see every collection
        # (a wsgidav `/d/` answers HEAD with a plain 200 and reads as a
        # file); a child from `iterdir()` carries its listing's is_dir hint,
        # so that route is covered. Use `dav:` for reliable detection.
        if self.is_dir():
            raise IsADirectoryError(
                _errno.EISDIR, _os.strerror(_errno.EISDIR), str(self)
            )
        self._delete(missing_ok=missing_ok)

    def _delete(self, missing_ok=False):
        # The raw DELETE, with no collection guard: `rmdir()` has already
        # verified an empty directory before calling it.
        with _translate_http_errors(self):
            resp = self.backend.request("DELETE", self)
            if resp.status_code == 404:
                if missing_ok:
                    return
                raise FileNotFoundError(self)
            resp.raise_for_status()

    def rmdir(self):
        # An empty directory's listing and a file whose body happens to
        # parse to zero entries are indistinguishable from `_listdir()`
        # alone -- without this check, rmdir() on a *file* silently
        # DELETEd it instead of raising NotADirectoryError like
        # os.rmdir()/pathlib.Path.rmdir() do.
        if not self.is_dir():
            raise NotADirectoryError(self)
        # `_scandir()`, not the raw `_listdir()`: a listing's own "."/".."
        # rows are not children and must not make an empty directory
        # look non-empty.
        for _ in self._scandir():
            raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
        self._delete()

    def with_session(
        self,
        session: _req.Session,
        write_method: str = "PUT",
        append_mode: str = "rewrite",
        **requests_args,
    ):
        return type(self)(
            self, backend=HttpBackend(session, requests_args, write_method, append_mode)
        )
