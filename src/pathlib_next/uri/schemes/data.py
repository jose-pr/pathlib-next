from __future__ import annotations

import io as _io
import urllib.parse as _urlparse
from base64 import b64decode as _b64decode

from ...utils.stat import FileStat
from .. import UriPath

_DEFAULT_MEDIATYPE = "text/plain;charset=US-ASCII"


class DataUri(UriPath):
    """`data:` scheme (RFC 2397): a read-only "file" whose entire content is
    embedded in the URI itself (`data:[<mediatype>][;base64],<data>`) -- no
    filesystem, no directories, no backend/connection. Percent-encoded
    reserved characters (`?`/`#`) in the payload are only decoded correctly
    if the URI was built with them escaped, per RFC 2397/3986."""

    __SCHEMES = ("data",)
    __slots__ = ()

    @property
    def _header(self) -> str:
        header, _, _ = self.path.partition(",")
        return header

    @property
    def _is_base64(self) -> bool:
        return self._header.rsplit(";", 1)[-1].strip().lower() == "base64"

    @property
    def mediatype(self) -> str:
        header = self._header
        if not header:
            return _DEFAULT_MEDIATYPE
        if self._is_base64:
            header = header.rsplit(";", 1)[0]
        return header or _DEFAULT_MEDIATYPE

    def _content(self) -> bytes:
        header, sep, data = self.path.partition(",")
        if not sep:
            raise FileNotFoundError(self)
        if self._is_base64:
            return _b64decode(_urlparse.unquote_to_bytes(data))
        return _urlparse.unquote_to_bytes(data)

    def stat(self, *, follow_symlinks=True):
        return FileStat(st_size=len(self._content()), is_dir=False)

    def _open(self, mode="r", buffering=-1):
        if "r" not in mode:
            raise NotImplementedError("data: URIs are read-only")
        return _io.BytesIO(self._content())

    def _listdir(self):
        raise NotADirectoryError(self)
