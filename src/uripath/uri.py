import os
from pathlib import PurePosixPath, PurePath, _ignore_error, Path as LocalPath
import typing as _ty
import uritools
import stat as _stat
import shutil as _shutil

if _ty.TYPE_CHECKING:
    from typing import Self

from . import utils as _utils, io as _io


_UriParsedResult = uritools.SplitResult


class UriPathSource(_ty.NamedTuple):
    scheme: str
    userinfo: str
    host: str
    port: int

    def __bool__(self):
        if not self.scheme:
            return False
        return True

    def parsed_userinfo(self):
        parts = []
        if self.userinfo:
            parts = self.userinfo.split(":", maxsplit=1)
        parts = parts + ["", ""]
        return parts[0], parts[1]

    @staticmethod
    def from_uri_parsed_result(parsed: _UriParsedResult):
        return UriPathSource(
            parsed.getscheme(),
            parsed.getuserinfo(),
            str(parsed.gethost() or ""),
            parsed.getport(),
        )


_NOSOURCE = UriPathSource(None, None, None, None)


class _UriPathParents(_ty.Sequence):
    """This object provides sequence-like access to the logical ancestors
    of a path.  Don't try to construct it yourself."""

    __slots__ = ("_path", "_parents")

    def __init__(self, path: "PureUriPath"):
        self._path = path
        self._parents = path.path.parents

    def __len__(self):
        return len(self._parents)

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return tuple(self[i] for i in range(*idx.indices(len(self))))
        return self._path.with_path(self._parents[idx])

    def __repr__(self):
        return "<{}.parents>".format(type(self._path).__name__)


class PureUriPath(object):
    """Base class for manipulating paths without I/O."""

    __slots__ = (
        # The `_raw_paths` slot stores unnormalized string paths. This is set
        # in the `__init__()` method.
        "_raw_paths",
        #
        #
        #
        "_source",
        "_path",
        "_query",
        "_fragment",
        "_uri",
        # The `_hash` slot stores the hash of the case-normalized string
        # path. It's set when `__hash__()` is called for the first time.
        "_hash",
    )

    def __reduce__(self):
        # Using the parts tuple helps share interned path parts
        # when pickling related paths.
        return (self.__class__, self.parts)

    def __init__(self, *args):
        paths: list[str] = []
        for arg in args:
            if isinstance(arg, PureUriPath):
                paths.append(arg.as_uri())
            elif isinstance(arg, PurePath):
                paths.append(arg.as_posix())
            else:
                try:
                    path = os.fspath(arg)
                except TypeError:
                    path = arg
                if not isinstance(path, str):
                    raise TypeError(
                        "argument should be a str or an os.PathLike "
                        "object where __fspath__ returns a str, "
                        f"not {type(path).__name__!r}"
                    )
                paths.append(path)
        self._raw_paths = paths
        self._load_parts()

    def with_segments(self, *pathsegments):
        """Construct a new path object from any number of path-like objects.
        Subclasses may override this method to customize how new path objects
        are created from methods like `iterdir()`.
        """
        return type(self)(*pathsegments)

    @classmethod
    def _parse_path(cls, path: str) -> tuple[UriPathSource, PurePosixPath, str, str]:
        parsed = uritools.urisplit(path)
        source = UriPathSource.from_uri_parsed_result(parsed)
        return (
            source,
            PurePosixPath(parsed.getpath()),
            parsed.getquery(),
            parsed.getfragment(),
        )

    def _load_parts(self):
        uris = self._raw_paths
        source = query = fragment = None
        paths = []

        for _uri in uris or [""]:
            src, path, query, fragment = self._parse_path(_uri)
            if src:
                source = src
                paths = [path]
            else:
                paths.append(path)

        self._source = source
        self._path = PurePosixPath(*paths)
        self._query = query
        self._fragment = fragment

    def _from_parsed_parts(self, source, path, query, fragment):
        path_str = self._format_parsed_parts(source, path, query, fragment)
        path = self.with_segments(path_str)
        path._uri = path_str or "."
        path._source = source
        path._path = path
        path._query = query
        path._fragment = fragment
        return path

    @classmethod
    def _format_parsed_parts(
        cls,
        source: UriPathSource,
        path: PurePosixPath,
        query,
        fragment,
        /,
        sanitize=True,
    ) -> str:
        last_path = (path._raw_paths or [""])[-1]
        suffix = "/" if last_path != "/" and last_path.endswith("/") else ""
        parts = {
            "path": path.as_posix() + suffix,
        }
        if query:
            parts["query"] = query
        if fragment:
            parts["fragment"] = fragment
        if source:
            source_ = source._asdict()
            if sanitize:
                source_["userinfo"] = (source_["userinfo"] or "").split(
                    ":", maxsplit=1
                )[0]
            parts.update(source_)

        return uritools.uricompose(**{k: v for k, v in parts.items() if v})

    def __str__(self):
        """Return the string representation of the path, suitable for
        passing to system calls."""
        return self.as_uri()

    def __fspath__(self):
        return self.as_uri()

    def __repr__(self):
        return "{}({!r})".format(self.__class__.__name__, self.as_uri())

    def as_uri(self, /, sanitize=True):
        try:
            return self._uri
        except AttributeError:
            self._uri = self._format_parsed_parts(
                self._source, self._path, self._query, self._fragment, sanitize=sanitize
            )
            return self._uri

    def as_posix(self):
        return self.path.as_posix()

    def as_localpath(self):
        return LocalPath(self.path)

    @property
    def source(self):
        try:
            return self._source
        except AttributeError:
            self._load_parts()
            return self._source

    @property
    def path(self):
        try:
            return self._path
        except AttributeError:
            self._load_parts()
            return self._path

    @property
    def query(self):
        try:
            return self._query
        except AttributeError:
            self._load_parts()
            return self._query

    @property
    def fragment(self):
        try:
            return self._fragment
        except AttributeError:
            self._load_parts()
            return self._fragment

    @property
    def anchor(self):
        return self.path.anchor

    @property
    def name(self):
        return self.path.name

    @property
    def suffix(self):
        return self.path.suffix

    @property
    def suffixes(self):
        return self.path.suffixes

    @property
    def stem(self):
        return self.path.stem

    def with_source(self, source):
        return self._from_parsed_parts(source, self.path, self.query, self.fragment)

    def with_path(self, path):
        return self._from_parsed_parts(
            self.source, PurePosixPath(path), self.query, self.fragment
        )

    def with_query(self, query: str):
        return self._from_parsed_parts(self.source, self.path, query, self.fragment)

    def with_fragment(self, fragment: str):
        return self._from_parsed_parts(self.source, self.path, self.query, fragment)

    def with_name(self, name):
        return self._from_parsed_parts(
            self.source, self.path.with_name(name), self.query, self.fragment
        )

    def with_stem(self, stem):
        """Return a new path with the stem changed."""
        return self.with_name(stem + self.suffix)

    def with_suffix(self, suffix):
        return self._from_parsed_parts(
            self.source, self.path.with_suffix(suffix), self.query, self.fragment
        )

    @property
    def parts(self):
        if self.source:
            return self.source, self.path, self.query, self.fragment
        else:
            return self.path, self.query, self.fragment

    def joinpath(self, *pathsegments):
        """Combine this path with one or several arguments, and return a
        new path representing either a subpath (if all arguments are relative
        paths) or a totally different path (if one of the arguments is
        anchored).
        """
        return self.with_segments(self, *pathsegments)

    def __truediv__(self, key):
        try:
            return self.joinpath(key)
        except TypeError:
            return NotImplemented

    def __rtruediv__(self, key):
        try:
            return self.with_segments(key, self)
        except TypeError:
            return NotImplemented

    @property
    def parent(self):
        """The logical parent of the path."""
        parent = self.path.parent
        if parent == self.path:
            return self
        return self.with_path(parent)

    @property
    def parents(self):
        """A sequence of this path's logical parents."""
        # The value of this property should not be cached on the path object,
        # as doing so would introduce a reference cycle.
        return _UriPathParents(self)

    def is_absolute(self):
        """True if the path is absolute (has both a root and, if applicable,
        a drive)."""
        return bool(self.source) or self.path.is_absolute()

    def is_relative_to(self, other):
        """Return True if the path is relative to another path or False."""
        other = self.with_segments(other)
        return other == self or other in self.parents

    def relative_to(self, other, /, walk_up=False):
        other = self.with_segments(other)
        if (self.source and other.source) and other.source != self.source:
            raise ValueError(f"{str(self)!r} is not in the subpath of {str(other)!r}")
        try:
            relpath = self.path.relative_to(other.path)
        except ValueError:
            relpath = self.path
        return self._from_parsed_parts(_NOSOURCE, relpath, self.query, self.fragment)


class UriPath(PureUriPath):
    __slots__ = ()
    _SCHEMES_: list[str] = []

    def __new__(cls, *args, **kwargs):
        if cls is UriPath:
            uri = PureUriPath(*args, **kwargs)
            if uri.source and uri.source.scheme:
                for scls in UriPath.__subclasses__():
                    if uri.source.scheme in scls._SCHEMES_:
                        cls = scls

        inst = object.__new__(cls)
        for slot in inst.__slots__:
            if not hasattr(inst, slot):
                setattr(inst, slot, None)
        return inst

    @_utils.notimplemented
    def iterdir(self) -> "_ty.Iterable[Self]": ...

    @_utils.notimplemented
    def stat(self) -> _io.FileStat: ...

    def is_dir(self):
        """
        Whether this path is a directory.
        """
        try:
            return _stat.S_ISDIR(self.stat().st_mode)
        except OSError as e:
            if not _ignore_error(e):
                raise
            return False
        except ValueError:
            return False

    def is_file(self):
        """
        Whether this path is a regular file (also True for symlinks pointing
        to regular files).
        """
        try:
            return _stat.S_ISREG(self.stat().st_mode)
        except OSError as e:
            if not _ignore_error(e):
                raise
            return False
        except ValueError:
            return False

    @_utils.notimplemented
    def _open(
        self,
        mode="r",
        buffering=-1,
    ) -> _io.IOBase:
        """
        All operations should be binary
        To be used only by UriPath.open() to obtain binary stream
        """
        ...

    def open(
        self, mode="r", buffering=-1, encoding=None, errors=None, newline=None
    ) -> _io.IOBase:
        fh = self._open(mode.replace("b", ""), buffering)
        if "b" not in mode:
            encoding = _io.text_encoding(encoding)
            fh = _io.TextIOWrapper(fh, encoding, errors, newline)
        return fh

    def read_bytes(self) -> bytes:
        """
        Open the file in bytes mode, read it, and close the file.
        """
        with self.open(mode="rb") as f:
            return f.read()

    def read_text(self, encoding=None, errors=None) -> str:
        with self.open(mode="r", encoding=encoding, errors=errors) as f:
            return f.read()

    def write_bytes(self, data):
        view = memoryview(data)
        with self.open(mode="wb") as f:
            return f.write(view)

    def write_text(self, data, encoding=None, errors=None, newline=None):
        if not isinstance(data, str):
            raise TypeError("data must be str, not %s" % data.__class__.__name__)
        encoding = _io.text_encoding(encoding)
        with self.open(
            mode="w", encoding=encoding, errors=errors, newline=newline
        ) as f:
            return f.write(data)

    def exists(self) -> bool:
        try:
            self.stat()
            return True
        except FileNotFoundError:
            return False

    def walk(self, top_down=True, on_error=None, follow_symlinks=False):
        """Walk the directory tree from this directory, similar to os.walk()."""
        paths: "list[UriPath|tuple[UriPath, list[str], list[str]]]" = [self]

        while paths:
            path = paths.pop()
            if isinstance(path, tuple):
                yield path
                continue
            try:
                scandir_it = path.iterdir()
            except OSError as error:
                if on_error is not None:
                    on_error(error)
                continue

            dirnames: "list[str]" = []
            filenames: "list[str]" = []
            for entry in scandir_it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                except OSError:
                    # Carried over from os.path.isdir().
                    is_dir = False

                if is_dir:
                    dirnames.append(entry.name)
                else:
                    filenames.append(entry.name)

            if top_down:
                yield path, dirnames, filenames
            else:
                paths.append((path, dirnames, filenames))

            paths += [path / d for d in reversed(dirnames)]

    @_utils.notimplemented
    def _mkdir(self, mode): ...

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        """
        Create a new directory at this given path.
        """
        try:
            self._mkdir(self, mode)
        except FileNotFoundError:
            if not parents or self.parent == self:
                raise
            self.parent.mkdir(parents=True, exist_ok=True)
            self.mkdir(mode, parents=False, exist_ok=exist_ok)
        except OSError:
            # Cannot rely on checking for EEXIST, since the operating system
            # could give priority to other errors like EACCES or EROFS
            if not exist_ok or not self.is_dir():
                raise

    @_utils.notimplemented
    def chmod(self, mode): ...

    @_utils.notimplemented
    def unlink(self, missing_ok=False): ...

    @_utils.notimplemented
    def rmdir(self): ...

    def rm(self, /, recursive=False, missing_ok=False, ignore_error=False):
        _onerror = lambda _err, _path: (
            ignore_error if not callable(ignore_error) else ignore_error
        )
        try:
            if not self.exists():
                if missing_ok:
                    return
                raise FileNotFoundError(self)
            if self.is_dir():
                if recursive:
                    for child in self.iterdir():
                        child.rm(recursive=recursive, ignore_error=ignore_error)
                self.rmdir()
            else:
                self.unlink()
        except Exception as error:
            if not _onerror(error, self):
                raise

    @_utils.notimplemented
    def _rename(self, target: PurePosixPath): ...

    def _src_dest(self, target):
        target = UriPath(target) if not isinstance(target, UriPath) else target
        src = self
        if not src.source:
            if target.source:
                src = src.with_source(target.source)
            else:
                raise FileNotFoundError(src)

        if not target._source:
            target = target.with_source(self.source)

        if target.source == src.source and target.path == src.path:
            return None, None

        return src, target

    def copy(self, target, *, overwrite=False):
        src, target = self._src_dest(target)
        if src is None:
            return

        if target.exists():
            if overwrite:
                target.unlink()
            else:
                raise FileExistsError(target)

        try:
            stat = src.stat()
        except NotImplementedError:
            stat = None

        with target.open("wb") as output, src.open("rb") as input:
            _shutil.copyfileobj(input, output)

        if stat:
            try:
                target.chmod(stat.st_mode)
            except NotImplementedError:
                pass

    def move(self, target, *, overwrite=False):
        src, target = self._src_dest(target)
        if src is None:
            return

        if target.exists():
            if overwrite:
                target.unlink()
            else:
                raise FileExistsError(target)

        if target.source == src.source:
            try:
                return src._rename(target.path)
            except NotImplementedError:
                pass

        src.copy(target)
        src.unlink()
