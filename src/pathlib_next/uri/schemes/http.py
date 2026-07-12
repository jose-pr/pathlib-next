from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import html.parser as _html_parser
import io as _io
import re as _re
import time as _time
import typing as _ty
import urllib.parse as _urlparse

import requests as _req

if _ty.TYPE_CHECKING:
    from urllib3.response import HTTPResponse

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import UriPath


@_contextlib.contextmanager
def _translate_http_errors(path_obj):
    try:
        yield
    except _req.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 404:
            raise FileNotFoundError(path_obj) from e
        elif status in (401, 403):
            raise PermissionError(path_obj) from e
        elif status == 409:
            raise FileExistsError(path_obj) from e
        elif status in (405, 501):
            raise PermissionError(f"Method not allowed for {path_obj}") from e
        else:
            raise OSError(_errno.EIO, f"HTTP Error {status} for {path_obj}") from e
    except _req.exceptions.Timeout as e:
        raise TimeoutError(f"Timeout for {path_obj}") from e
    except _req.exceptions.ConnectionError as e:
        raise ConnectionError(f"Connection error for {path_obj}") from e
    except _req.exceptions.RequestException as e:
        raise OSError(_errno.EIO, f"Request failed for {path_obj}") from e


_RE_ISO8601 = _re.compile(r'\d{4}-\d+-\d+T\d+:\d{2}:\d{2}Z')
_DATETIME_FMTs = (
    (_re.compile(r'\d+-[A-S][a-y]{2}-\d{4} \d+:\d{2}:\d{2}'), "%d-%b-%Y %H:%M:%S"),
    (_re.compile(r'\d+-[A-S][a-y]{2}-\d{4} \d+:\d{2}'), "%d-%b-%Y %H:%M"),
    (_re.compile(r'\d{4}-\d+-\d+ \d+:\d{2}:\d{2}'), "%Y-%m-%d %H:%M:%S"),
    (_RE_ISO8601, "%Y-%m-%dT%H:%M:%SZ"),
    (_re.compile(r'\d{4}-\d+-\d+ \d+:\d{2}'), "%Y-%m-%d %H:%M"),
    (_re.compile(r'\d{4}-[A-S][a-y]{2}-\d+ \d+:\d{2}:\d{2}'), "%Y-%b-%d %H:%M:%S"),
    (_re.compile(r'\d{4}-[A-S][a-y]{2}-\d+ \d+:\d{2}'), "%Y-%b-%d %H:%M"),
    (_re.compile(r'[F-W][a-u]{2} [A-S][a-y]{2} +\d+ \d{2}:\d{2}:\d{2} \d{4}'), "%a %b %d %H:%M:%S %Y"),
    (_re.compile(r'[F-W][a-u]{2}, \d+ [A-S][a-y]{2} \d{4} \d{2}:\d{2}:\d{2} \S+'), "%a, %d %b %Y %H:%M:%S %Z"),
    (_re.compile(r'\d{4}-\d+-\d+'), "%Y-%m-%d"),
    (_re.compile(r'\d+/\d+/\d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}'), "%d/%m/%Y %H:%M:%S %z"),
    (_re.compile(r'\d{2} [A-S][a-y]{2} \d{4}'), "%d %b %Y")
)

_RE_FILESIZE = _re.compile(r'\d[\d,]*(\.\d+)? ?[BKMGTPEZY]|\d[\d,]*|-', _re.I)
_RE_COMMONHEAD = _re.compile('Name|(Last )?modifi(ed|cation)|date|Size|Description|Metadata|Type|Parent Directory', _re.I)
_RE_HEAD_NAME = _re.compile('name$|^file|^download')
_RE_HEAD_MOD = _re.compile('modifi|^uploaded|date|time')
_RE_HEAD_SIZE = _re.compile('size|bytes$')


def _human2bytes(s):
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        symbols = 'BKMGTPEZY'
        letter = s[-1:].strip().upper()
        num = float(s[:-1])
        prefix = {symbols[0]: 1}
        for i, sym in enumerate(symbols[1:]):
            prefix[sym] = 1 << (i+1)*10
        return int(num * prefix.get(letter, 1))


def _aherf2filename(a_href):
    isdir = ('/' if a_href.endswith('/') else '')
    path = _urlparse.urlsplit(a_href).path
    return _urlparse.unquote(path.rstrip('/')).rsplit('/', 1)[-1] + isdir


class _DirectoryListingParser(_html_parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.listing = []
        
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
        if tag == 'title':
            self.in_title = True
            self.title_text = ""
        elif tag == 'pre':
            self.in_pre = True
        elif tag == 'table':
            self.in_table = True
            self.table_rows = []
            self.headers = None
        elif tag == 'tr' and self.in_table:
            self.in_tr = True
            self.current_row = []
        elif (tag == 'td' or tag == 'th') and self.in_tr:
            self.in_td = True
            self.current_cell_text = []
            self.current_cell_href = None
        elif tag == 'a':
            self.in_a = True
            href = attrs_dict.get('href')
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
        if tag == 'title':
            self.in_title = False
            title = self.title_text.strip()
            if title.startswith('Index of '):
                self.cwd = title[9:]
        elif tag == 'pre':
            self._flush_pre_entry()
            self.in_pre = False
        elif tag == 'table':
            self.in_table = False
            self._process_table()
        elif tag == 'tr' and self.in_tr:
            self.in_tr = False
            self.table_rows.append(self.current_row)
        elif (tag == 'td' or tag == 'th') and self.in_td:
            self.in_td = False
            cell_text = "".join(self.current_cell_text).strip()
            self.current_row.append((cell_text, self.current_cell_href))
        elif tag == 'a':
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

    def _is_ancestor_href(self, href):
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
        if not href.startswith('/'):
            return False
        if not self.cwd:
            return True
        path = _urlparse.unquote(_urlparse.urlsplit(href).path)
        cwd = self.cwd if self.cwd.endswith('/') else self.cwd + '/'
        # a strict descendant of cwd is a real child; cwd itself (a
        # self-referencing "up" link, e.g. at the site root) or anything
        # outside cwd is the ancestor/parent link.
        return path == cwd or not path.startswith(cwd)

    def _flush_pre_entry(self):
        if not self.last_href:
            return

        name = _aherf2filename(self.last_href)
        if (
            name in ('Parent Directory', '..', '../')
            or self.last_href.startswith('?')
            or self._is_ancestor_href(self.last_href)
        ):
            self.last_href = None
            return
            
        modified = None
        size = None
        description = None
        
        text = "".join(self.pre_data_buffer).replace('\r', '').split('\n', 1)[0].lstrip()
        if text:
            for regex, fmt in _DATETIME_FMTs:
                match = regex.match(text)
                if match:
                    try:
                        modified = _time.strptime(match.group(0), fmt)
                    except ValueError:
                        pass
                    text = text[match.end():].lstrip()
                    break
            
            match = _RE_FILESIZE.match(text)
            if match:
                sizestr = match.group(0)
                if sizestr != '-':
                    size = _human2bytes(sizestr.replace(' ', '').replace(',', ''))
                text = text[match.end():].lstrip()
                
            if text:
                description = text.rstrip()
                if description == '/':
                    name += '/'
                    description = None
                    
        self.listing.append(_FileEntry(name, modified, size, description))
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
                    norm = text.strip(' \t\n\r\x0b\x0c\xa0↑↓').lower()
                    if not norm:
                        continue
                    if not name_found and _RE_HEAD_NAME.search(norm):
                        self.headers.append('name')
                        name_found = True
                    elif norm in ('size', 'description'):
                        self.headers.append(norm)
                    elif _RE_HEAD_MOD.search(norm):
                        self.headers.append('modified')
                    elif _RE_HEAD_SIZE.search(norm):
                        self.headers.append('size')
                    elif norm.endswith('signature'):
                        self.headers.append('signature')
                    else:
                        self.headers.append('description')
                if not self.headers:
                    self.headers = ['name', 'modified', 'size', 'description']
                elif not name_found:
                    self.headers[0] = 'name'
                started = True
                continue
                
            if started:
                file_name = None
                file_mod = None
                file_size = None
                file_desc = None
                
                status = 0
                for cell_text, cell_href in row:
                    if status >= len(self.headers):
                        break
                    
                    header = self.headers[status]
                    if header == 'name':
                        if not cell_href or cell_href.startswith('#'):
                            continue
                        name_val = cell_text.strip()
                        if name_val == 'Parent Directory' or cell_href == '../':
                            break
                        file_name = _aherf2filename(cell_href)
                        status = 1
                    elif header == 'modified':
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
                    elif header == 'size':
                        sizestr = cell_text.strip().replace(',', '')
                        if sizestr and sizestr != '-':
                            match = _RE_FILESIZE.match(sizestr)
                            if match:
                                file_size = _human2bytes(match.group(0).replace(' ', ''))
                        status += 1
                    elif header == 'description':
                        file_desc = cell_text or None
                        status += 1
                    else:
                        status += 1
                        
                if file_name:
                    self.listing.append(_FileEntry(file_name, file_mod, file_size, file_desc))

    def close(self):
        super().close()
        self._flush_pre_entry()
        if not self.listing:
            for text, href in self.all_links:
                name = _aherf2filename(href)
                if (
                    name in ('Parent Directory', '..', '../')
                    or href.startswith('?')
                    or self._is_ancestor_href(href)
                ):
                    continue
                self.listing.append(_FileEntry(name, None, None, None))




class _FileEntry(_ty.NamedTuple):
    name: str
    modified: _ty.Optional[_time.struct_time]
    size: _ty.Optional[int]
    description: _ty.Optional[str]


class HttpWriteStream(_io.BytesIO):
    def __init__(self, path: "HttpPath"):
        super().__init__()
        self._path = path

    def close(self):
        if self.closed:
            return
        data = self.getvalue()
        try:
            with _translate_http_errors(self._path):
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


class HttpBackend(_ty.NamedTuple):
    """Per-instance `requests.Session` + extra request kwargs shared by an
    `HttpPath` tree (see `with_session()`)."""

    session: _req.Session
    requests_args: dict
    write_method: str = "PUT"

    def request(self, method, uri: "HttpPath|str", **kwargs):
        return self.session.request(
            **self.requests_args,
            **kwargs,
            method=method,
            url=uri if isinstance(uri, str) else uri.as_uri(False),
        )


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
            with _translate_http_errors(self):
                req = self.backend.request("GET", self)
                req.raise_for_status()
        except FileNotFoundError:
            if self.path.endswith("/"):
                raise
            with _translate_http_errors(self):
                req = self.backend.request("GET", self.with_path(self.path + "/"))
                req.raise_for_status()
        parser = _DirectoryListingParser()
        parser.feed(req.text)
        parser.close()
        return parser.listing

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
            if not name:
                continue
            yield name, FileStat(
                st_size=0 if is_dir else (entry.size or 0),
                st_mtime=_utils.parsedate(entry.modified),
                is_dir=is_dir,
            )

    def _is_dir(self, resp: _req.Response):
        return (
            resp.is_redirect
            or resp.url.endswith("/")
            or resp.url.endswith("/..")
            or resp.url.endswith("/.")
        )

    def stat(self, *, follow_symlinks=True, walk_up_last_modified=False):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint

        with _translate_http_errors(self):
            check = (
                [self.with_path(self.path.removesuffix("/")), self]
                if self.path.endswith("/")
                else [self]
            )
            for uri in check:
                resp = self.backend.request("HEAD", uri, allow_redirects=False)
                resp.close()
                if resp.status_code == 405:
                    # Some servers reject HEAD outright; fall back to GET.
                    resp = self.backend.request(
                        "GET", uri, allow_redirects=False, stream=True
                    )
                    resp.close()
                if resp.status_code < 400:
                    break

            # is_dir is intentionally derived from this pre-redirect resp,
            # not re-derived after following the redirect below: a 3xx
            # response's own `resp.is_redirect` is already sufficient (and
            # is the only signal available pre-redirect), and the target
            # should independently satisfy `_is_dir()`'s url.endswith("/")
            # check too once fetched.
            is_dir = self._is_dir(resp)

            if resp.is_redirect:
                resp = self.backend.request("HEAD", uri)
                resp.close()
                if resp.status_code == 405:
                    # Mirror the pre-redirect loop's HEAD-405 fallback --
                    # without this, a server/proxy that rejects HEAD
                    # everywhere (not just pre-redirect) surfaced
                    # PermissionError for an existing, redirect-only path.
                    resp = self.backend.request("GET", uri, stream=True)
                    resp.close()
            resp.raise_for_status()

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
            buffer_size = _io.DEFAULT_BUFFER_SIZE if buffering < 0 else buffering
            with _translate_http_errors(self):
                req = self.backend.request("GET", self.as_uri(), stream=True)
                req.raise_for_status()
            resp: "HTTPResponse" = req.raw
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
        return HttpWriteStream(self)

    def unlink(self, missing_ok=False):
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
        for _ in self._listdir():
            raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
        self.unlink()

    def with_session(self, session: _req.Session, write_method: str = "PUT", **requests_args):
        return type(self)(self, backend=HttpBackend(session, requests_args, write_method))
