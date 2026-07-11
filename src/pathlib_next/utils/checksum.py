from __future__ import annotations

import hashlib as _hashlib
import typing as _ty

if _ty.TYPE_CHECKING:
    from ..path import Path


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
