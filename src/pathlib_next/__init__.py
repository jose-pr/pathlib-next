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

try:
    from .uri import Uri as Uri
    from .uri import UriPath as UriPath
except ImportError:
    pass
else:
    __all__ += ["Uri", "UriPath"]
from .fspath import LocalPath, PosixPathname, WindowsPathname
from .path import FsPathLike, Path, PathLike, Pathname, PurePathLike
from .protocols import BinaryOpen, Chmod, Stat
from .utils import glob as glob
from .utils import sync as sync
from .utils.stat import FileStat
