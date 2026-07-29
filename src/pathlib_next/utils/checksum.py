from __future__ import annotations

import hashlib as _hashlib
import typing as _ty

if _ty.TYPE_CHECKING:
    from ..path import Path


def native(path: "Path", algorithm: str = "md5") -> "str | None":
    """Try `path`'s backend-native `NativeChecksum.checksum()` (see
    `protocols/checksum.py`); return `None` if `path` doesn't implement the
    protocol at all, or if it does but can't produce `algorithm` (raises
    `NotImplementedError` -- the protocol's documented contract for
    "unsupported", never a wrong-algorithm value). Never raises for either
    of those two cases; any other exception the backend raises propagates.
    """
    checksum = getattr(path, "checksum", None)
    if checksum is None:
        return None
    try:
        return checksum(algorithm)
    except NotImplementedError:
        return None


def md5(path: Path, chunk_size: int = 65536) -> str:
    """Calculate MD5 checksum of the file at `path`."""
    h = _hashlib.md5()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256(path: Path, chunk_size: int = 65536) -> str:
    """Calculate SHA-256 checksum of the file at `path`."""
    h = _hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def stream(path: "Path", algorithm: str = "md5", chunk_size: int = 65536) -> str:
    """Streaming checksum for an arbitrary `hashlib` `algorithm` name (the
    generic form of `md5`/`sha256` above, used where the algorithm is a
    runtime parameter rather than fixed at the call site -- e.g.
    `PathSyncer`'s streaming fallback, which must match whatever algorithm
    a native checksum attempt was made under).
    """
    h = _hashlib.new(algorithm)
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
