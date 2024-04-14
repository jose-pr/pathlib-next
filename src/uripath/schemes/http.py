from ..uri import Uri
from .. import fs, utils as _utils
import io as _io
import requests as _req
import typing as _ty
import bs4 as _bs4
import time as _time

from htmllistparse import parse as _htmlparse


class _FileEntry(_ty.NamedTuple):
    name: str
    modified: _ty.Optional[_time.struct_time]
    size: _ty.Optional[int]
    description: _ty.Optional[str]


class HttpBackend(_ty.NamedTuple):
    session: _req.Session
    requests_args: dict

    def request(self, method, uri: "HttpPath", **kwargs):
        return self.session.request(
            method, **self.requests_args, url=str(uri), **kwargs
        )


class HttpPath(Uri):
    _SCHEMES_ = ["http", "https"]
    __slots__ = ("_isdir", "_session", "_requests_args")
    _isdir: bool

    if _ty.TYPE_CHECKING:
        backend: HttpBackend

    def _initbackend(self):
        return HttpBackend(_req.Session(), {})

    def _ls(self) -> list[_FileEntry]:
        req = self.backend.session.request("GET", self)
        req.raise_for_status()
        soup = _bs4.BeautifulSoup(req.content, "html5lib")
        _, listing = _htmlparse(soup)
        return listing

    def iterdir(self):
        for child in self._ls():
            path = self / child.name
            path._isdir = child.name.endswith("/")
            yield path

    def _is_dir(self, resp: _req.Response):
        return (
            resp.status_code in (301, 302)
            or resp.url.endswith("/")
            or resp.url.endswith("/..")
            or resp.url.endswith("/.")
        )

    def stat(self):
        session = self.backend.session
        url = self.as_uri()
        resp = session.head(
            url.rstrip("/"), **self.backend.requests_args, allow_redirects=False
        )
        resp.close()
        entry = None

        if resp.status_code == 404:
            raise FileNotFoundError(url)
        elif resp.status_code == 403:
            raise PermissionError(url)
        elif resp.status_code in (301, 302):
            pass
        else:
            resp.raise_for_status()

        if self._isdir is None:
            self._isdir = self._is_dir(resp)

        st_size = int(resp.headers.get("Content-Length", 0))
        lm = resp.headers.get("Last-Modified")
        if lm is None:
            parent = self.parent
            if self.path.parts[-1] != parent.path.parts[-1]:
                try:
                    entry = next(
                        filter(
                            lambda p: p.name == self.name,
                            parent._ls(),
                        )
                    )
                    if entry and entry.modified:
                        lm = entry.modified
                except:
                    pass

        return fs.FileStat(
            st_size=st_size, st_mtime=_utils.parsedate(lm), is_dir=self._isdir
        )

    def _open(
        self,
        mode="r",
        buffering=-1,
        encoding=None,
    ):
        buffer_size = _io.DEFAULT_BUFFER_SIZE if buffering < 0 else buffering
        req = self.backend.request("GET", self.as_uri(), stream=True)
        return (
            req.raw
            if buffer_size == 0
            else _io.BufferedReader(req.raw, buffer_size=buffer_size)
        )

    def is_dir(self):
        if getattr(self, "_isdir", None) is None:
            self.stat()
        return self._isdir

    def is_file(self):
        return not self.is_dir()

    def with_session(self, session: _req.Session, **requests_args):
        return self.__class__(self, backend=HttpBackend(session, requests_args))
