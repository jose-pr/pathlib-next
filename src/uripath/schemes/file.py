from pathlib import Path as _Path
import os as _os
from ..uri import Uri, UriSource

class FileUri(Uri):
    __SCHEMES = ("file",)
    __slots__ = ("_filepath",)

    @property
    def filepath(self):
        if self._filepath is None:
            if not self.is_local():
                raise FileNotFoundError(self)
            self._filepath = _Path(self.path)
        return self._filepath
    
    def _init(
        self,
        source: UriSource,
        path: str,
        query: str,
        fragment: str,
        /,
        **kwargs,
    ):
        if _os.name == "nt" and path and path[0] == "/":
            root, *_ = path.split("/", maxsplit=1)
            if root[-1] == ":":
                path = path.removeprefix("/")
        super()._init(source, path, query, fragment, **kwargs)


    def _ls(self):
        for path in self.filepath.iterdir():
            yield path.name

    def stat(self, *, follow_symlinks=True):
        return self.filepath.stat(follow_symlinks=follow_symlinks)

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        return self.filepath.open(mode, buffering, encoding, errors, newline)

    def mkdir(self, mode=511, parents=False, exist_ok=False):
        return self.filepath.mkdir(mode, parents, exist_ok)

    def chmod(self, mode, *, follow_symlinks=True):
        return self.filepath.chmod(mode, follow_symlinks=follow_symlinks)

    def unlink(self, missing_ok=False):
        return self.filepath.unlink(missing_ok)

    def rmdir(self):
        return self.filepath.rmdir()

    def _rename(self, target):
        return self.filepath.rename(target)
