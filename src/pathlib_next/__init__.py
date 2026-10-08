from __future__ import annotations

from importlib import metadata as _metadata

__all__ = [
    "BinaryOpen",
    "Chmod",
    "FileStat",
    "FsPathLike",
    "LocalPath",
    "Path",
    "PathLike",
    "Pathname",
    "PosixPathname",
    "PurePathLike",
    "Stat",
    "WindowsPathname",
    "__version__",
    "glob",
    "sync",
]

try:
    __version__ = _metadata.version("pathlib-next")
except _metadata.PackageNotFoundError:  # a source tree that is not installed
    __version__ = "0+unknown"

_uri_unavailable: "ImportError | None" = None
try:
    from .uri import Uri as Uri
    from .uri import UriPath as UriPath
except ImportError as _error:
    _uri_unavailable = _error
else:
    __all__ += ["Uri", "UriPath"]


def __getattr__(name: str):
    # `hasattr(pathlib_next, "UriPath")` stays False without the extra: this
    # is an AttributeError that says why.
    if name in ("Uri", "UriPath") and _uri_unavailable is not None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}: {_uri_unavailable}"
        ) from _uri_unavailable
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from .fspath import LocalPath, PosixPathname, WindowsPathname
from .path import FsPathLike, Path, PathLike, Pathname, PurePathLike
from .protocols import BinaryOpen, Chmod, Stat
from .utils import glob as glob
from .utils import sync as sync
from .utils.stat import FileStat
