from __future__ import annotations

import io as _io
from base64 import b64decode as _b64decode

from ...utils.stat import FileStat
from .. import UriPath
from ..source import _ERRORS

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
        # RFC 2397: the extension is ";base64". "data:base64,..." has none;
        # there "base64" is the (malformed) media type, not an encoding.
        params = self._header.rsplit(";", 1)
        return len(params) == 2 and params[1].strip().lower() == "base64"

    @property
    def mediatype(self) -> str:
        """The declared media type. Without a type/subtype, RFC 2397 implies
        "text/plain": a bare header gives "text/plain;charset=US-ASCII",
        and parameters alone (";charset=utf-8") are given that type."""
        header = self._header
        if self._is_base64:
            header = header.rsplit(";", 1)[0]
        if not header:
            return _DEFAULT_MEDIATYPE
        if header.startswith(";"):
            return "text/plain" + header
        return header

    def _content(self) -> bytes:
        header, sep, data = self.path.partition(",")
        if not sep:
            raise FileNotFoundError(self)
        # `.path` is already percent-decoded exactly once (and, for data:,
        # not dot-normalized), with any non-UTF-8 byte carried as a
        # surrogate -- so this re-encodes rather than unquoting a second
        # time, which turned "100%2525" into b"100%" instead of b"100%25".
        payload = data.encode("utf-8", _ERRORS)
        if self._is_base64:
            return _b64decode(payload)
        return payload

    def stat(self, *, follow_symlinks=True):
        return FileStat(st_size=len(self._content()), is_dir=False)

    def _open(self, mode="r", buffering=-1):
        # Exactly "r": "r+" returned a writable buffer whose writes were
        # silently discarded.
        if mode != "r":
            raise NotImplementedError("data: URIs are read-only")
        return _io.BufferedReader(_io.BytesIO(self._content()))

    def _listdir(self):
        raise NotADirectoryError(self)
