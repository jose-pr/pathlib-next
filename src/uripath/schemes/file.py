from pathlib import Path as _Path
import typing as _ty
from ..uri import Uri


class FileUri(Uri):
    _SCHEMES_ = ["file"]
    _PATH_CLS = _Path

    if _ty.TYPE_CHECKING:
        path:_Path
    
    def iterdir(self):
        for path in self.path.iterdir():
            return self.__class__(path)

    def stat(self, *, follow_symlinks=True):
        return self.path.stat(follow_symlinks=follow_symlinks)

    def open(
        self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
    ):
        return self.path.open(mode, buffering, encoding, errors, newline)

    def mkdir(self, mode=511, parents=False, exist_ok=False):
        return self.path.mkdir(mode, parents, exist_ok)

    def chmod(self, mode, *, follow_symlinks=True):
        return self.path.chmod(mode, follow_symlinks=follow_symlinks)

    def unlink(self, missing_ok=False):
        return self.path.unlink(missing_ok)

    def rmdir(self):
        return self.path.rmdir()
    
    def _rename(self, target):
        return self.path.rename(target)