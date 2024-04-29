import io as _io
import time as _time
import typing as _ty

import bs4 as _bs4
import requests as _req
from htmllistparse import parse as _htmlparse

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import UriPath


class _FileEntry(_ty.NamedTuple):
    name: str
    modified: _ty.Optional[_time.struct_time]
    size: _ty.Optional[int]
    description: _ty.Optional[str]


class HttpBackend(_ty.NamedTuple):
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
    __SCHEMES = ("http", "https")
    __slots__ = ("_isdir", "_session", "_requests_args")
    _isdir: bool

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

    def iterdir(self):
        _self = self.path.removesuffix("/")
        cls = type(self)
        for path in self._listdir():
            inst = type(self).__new__(cls, backend=self.backend)
            inst._init(self.source, f"{_self}/{path.name}", "", "")
            yield inst

    def _is_dir(self, resp: _req.Response):
        return (
            resp.is_redirect
            or resp.url.endswith("/")
            or resp.url.endswith("/..")
            or resp.url.endswith("/.")
        )

    def stat(self):
        check = (
            [self.with_path(self.path.removesuffix("/")), self]
            if self.path.endswith("/")
            else [self]
        )
        for uri in check:
            resp = self.backend.request("HEAD", uri, allow_redirects=False)
            resp.close()
            if resp.status_code < 400:
                break

        if self._isdir is None:
            self._isdir = self._is_dir(resp)

        if resp.is_redirect:
            resp = self.backend.request("HEAD", uri)
        if resp.status_code == 404:
            raise FileNotFoundError(self)
        elif resp.status_code == 403:
            raise PermissionError(self)
        else:
            resp.raise_for_status()

        st_size = 0 if self._isdir else int(resp.headers.get("Content-Length", 0))
        lm = resp.headers.get("Last-Modified")
        if lm is None:
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
                except:
                    pass

        return FileStat(
            st_size=st_size, st_mtime=_utils.parsedate(lm), is_dir=self._isdir
        )

    def _open(
        self,
        mode="r",
        buffering=-1,
    ):
        if mode != "r":
            raise NotImplementedError(mode)
        buffer_size = _io.DEFAULT_BUFFER_SIZE if buffering < 0 else buffering
        req = self.backend.request("GET", self.as_uri(), stream=True)
        return (
            req.raw
            if buffer_size == 0
            else _io.BufferedReader(req.raw, buffer_size=buffer_size)
        )

    def is_dir(self):
        if self._isdir is None:
            self.stat()
        return self._is_dir is not None and self._isdir

    def is_file(self):
        return self.is_dir is not None and not self.is_dir()

    def with_session(self, session: _req.Session, **requests_args):
        return type(self)(self, backend=HttpBackend(session, requests_args))
