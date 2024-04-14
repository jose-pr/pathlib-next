from .uri import UriPath
from . import utils as _utils, io as _io
import posixpath as _posix
import requests as _req


class HttpPath(UriPath):
    __slots__ = ("_isdir", "_session", "_requests_args")
    _isdir: bool
    _session: _req.Session
    _requests_args: dict

    _SCHEMES_ = ['http', 'https']

    @property
    def session(self) -> _req.Session:
        if not getattr(self, "_session", None):
            self._session = _req.Session()
        return self._session

    @property
    def requests_args(self) -> dict:
        if not getattr(self, "_requests_args", None):
            self._requests_args = {}
        return self._requests_args

    def _dirls_(self):
        return _utils.ls(self.as_uri(), self.session, **self.requests_args)

    def iterdir(self):
        for child in self._dirls_():
            path = self / child.name
            path._isdir = child.name.endswith("/")
            yield path

    def stat(self):
        session = self.session
        url = self.as_uri()
        req = session.head(url.rstrip("/"), **self.requests_args, allow_redirects=False)
        req.close()
        entry = None
        if getattr(self, '_isdir', None) is None:
            self._isdir = (
                req.status_code in (301, 302)
                or url.endswith("/")
                or url.endswith("/..")
                or url.endswith("/.")
            )
        if req.status_code == 404:
            raise FileNotFoundError(url)
        elif req.status_code == 403:
            raise PermissionError(url)
        elif req.status_code in (301, 302):
            pass
        else:
            req.raise_for_status()
        st_size = int(req.headers.get("Content-Length", 0))
        lm = req.headers.get("Last-Modified")
        if lm is None:
            parent = self.parent
            if self.path.parts[-1] != parent.path.parts[-1]:
                try:
                    entry = next(
                        filter(
                            lambda p: p.name == self.name,
                            parent._dirls_(),
                        )
                    )
                    if entry and entry.modified:
                        lm = entry.modified
                except:
                    pass

        return _io.FileStat(
            st_size=st_size, st_mtime=_utils.parsedate(lm), is_dir=self._isdir
        )

    def _open(
        self,
        mode="r",
        buffering=-1,
        encoding=None,
        /,
        session: _req.Session = None,
        requests_args=None,
    ):
        buffer_size = _io.DEFAULT_BUFFER_SIZE if buffering < 0 else buffering
        session = session or self.session
        requests_args = requests_args if requests_args is not None else self.requests_args
        req = session.get(self.as_uri(), **requests_args, stream=True)
        return req.raw if buffer_size == 0 else _io.BufferedReader(req.raw, buffer_size=buffer_size)


    def is_dir(self):
        if getattr(self, '_isdir', None) is None:
            self.stat()
        return self._isdir

    def is_file(self):
        return not self.is_dir()

    def with_segments(self, *pathsegments):
        r = super().with_segments(*pathsegments)
        session = self.session
        args = self.requests_args
        for p in reversed(pathsegments):
            if isinstance(p, HttpPath):
                session = p.session
                args = p.requests_args
                break
        r._session = session
        r._requests_args = args
        return r

    def with_session(self, session: _req.Session, **requests_args):
        r = HttpPath(self)
        r._session = session
        r._requests_args = requests_args
        return r
