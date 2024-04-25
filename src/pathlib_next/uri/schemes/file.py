from ...fspath import FSPath as _SystemPath
from ...path import FsPathLike
import os as _os
from .. import Uri, Source


class FileUri(Uri):
    __SCHEMES = ("file",)
    __slots__ = ("_filepath",)

    @property
    def filepath(self):
        if self._filepath is None:
            if not self.is_local():
                if _os.name != "net":
                    raise NotImplementedError(
                        "Remote Source only supported in local paths in Windows"
                    )
                path = self.path if self.path.startswith("/") else f"/{self.path}"
                self._filepath = _SystemPath(f"//{self.source.host}{path}")
            else:
                self._filepath = _SystemPath(self.path)
        return self._filepath

    def _init(
        self,
        source: Source,
        path: str,
        query: str,
        fragment: str,
        /,
        **kwargs,
    ):
        if _os.name == "nt" and path and path[0] == "/":
            root, *_ = path[1:].split("/", maxsplit=1)
            if root and root[-1] == ":":
                path = path.removeprefix("/")
        super()._init(source, path, query, fragment, **kwargs)

    def __fspath__(self):
        return self.filepath.__fspath__()

    def _listdir(self):
        yield from _os.listdir(self.filepath)

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

    def rename(self, target: FsPathLike | str):
        try:
            _target = _os.fspath(target)
        except (TypeError, NotImplementedError):
            _target = NotImplemented

        if _target is NotImplemented:
            raise NotImplementedError("rename", target)

        return self.filepath.rename(_target)
