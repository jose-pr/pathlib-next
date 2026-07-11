from __future__ import annotations

import io as _io
import time as _time
import typing as _ty

import bs4 as _bs4
import requests as _req
from htmllistparse import parse as _htmlparse

if _ty.TYPE_CHECKING:
    from urllib3.response import HTTPResponse

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import UriPath


class _FileEntry(_ty.NamedTuple):
    name: str
    modified: _ty.Optional[_time.struct_time]
    size: _ty.Optional[int]
    description: _ty.Optional[str]


class HttpBackend(_ty.NamedTuple):
    """Per-instance `requests.Session` + extra request kwargs shared by an
    `HttpPath` tree (see `with_session()`)."""

    session: _req.Session
    requests_args: dict

    def request(self, method, uri: "HttpPath|str", **kwargs):
        return self.session.request(
            **self.requests_args,
            **kwargs,
            method=method,
            url=uri if isinstance(uri, str) else uri.as_uri(False),
        )


class HttpPath(UriPath):
    """`http`/`https` scheme: read-only access over HTTP, listing
    directories by scraping an Apache/nginx-style HTML index (via
    `htmllistparse`). Requires the `http` extra."""

    __SCHEMES = ("http", "https")
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: HttpBackend

    def _initbackend(self):
        return HttpBackend(_req.Session(), {})

    def _listdir(self) -> list[_FileEntry]:
        req = self.backend.request("GET", self)
        req.raise_for_status()
        soup = _bs4.BeautifulSoup(req.content, "html5lib")
        _, listing = _htmlparse(soup)
        return listing

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

        is_dir = self._is_dir(resp)

        if resp.is_redirect:
            resp = self.backend.request("HEAD", uri)
            resp.close()
        if resp.status_code == 404:
            raise FileNotFoundError(self)
        elif resp.status_code == 403:
            raise PermissionError(self)
        else:
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
        if mode != "r":
            raise NotImplementedError(mode)
        buffer_size = _io.DEFAULT_BUFFER_SIZE if buffering < 0 else buffering
        req = self.backend.request("GET", self.as_uri(), stream=True)
        resp: "HTTPResponse" = req.raw
        resp.auto_close = False
        return (
            resp
            if buffer_size == 0
            else _io.BufferedReader(resp, buffer_size=buffer_size)
        )

    def with_session(self, session: _req.Session, **requests_args):
        return type(self)(self, backend=HttpBackend(session, requests_args))
