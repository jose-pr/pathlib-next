from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import io as _io
import typing as _ty

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import Uri, UriPath


class BaseGsBackend(object):
    """Protocol for obtaining a `google.cloud.storage` Client. Subclass this
    to plug in custom credential/session handling (e.g. tests can override to
    point at a fake server); `GsBackend` is the real implementation."""

    __slots__ = ()

    @_utils.notimplemented
    def client(self): ...


class GsBackend(BaseGsBackend):
    """Lazily creates+caches a `google.cloud.storage` Client. A single client
    is reused across threads -- it's documented as thread-safe.

    `client_kwargs` go to `storage.Client` unchanged, `client_options`
    (dict or `ClientOptions`, `api_endpoint` included) among them. A custom
    endpoint still authenticates; for an emulator or fake server pass
    `use_auth_w_custom_endpoint=False` (anonymous credentials), or set
    `STORAGE_EMULATOR_HOST` yourself -- the SDK reads it."""

    __slots__ = ("client_kwargs", "_client")

    def __init__(self, **client_kwargs):
        self.client_kwargs = client_kwargs
        self._client = None

    def client(self):
        if self._client is None:
            from google.cloud import storage

            # Passed through as given. This used to turn a dict
            # `api_endpoint` into a process-wide, never-restored
            # `os.environ["STORAGE_EMULATOR_HOST"]` and drop the other
            # client options: every later client in the process (and any
            # subprocess) was redirected to that endpoint with anonymous
            # credentials.
            self._client = storage.Client(**self.client_kwargs)
        return self._client


def _object_key(path: str) -> str:
    """The object key a gs URI path names: leading `/`s dropped, and
    exactly one trailing `/`. `gs://b/dir/` is the directory `dir`, the
    way pathlib drops a trailing slash -- keeping it made the key `dir/`, so
    the `dir/` marker object read as a file and `rm(recursive=True)` deleted
    only the marker. Interior empty segments (`a//b`) are literal key bytes
    and stay."""
    key = path.lstrip("/")
    return key[:-1] if key.endswith("/") else key


def _http_status(error: BaseException) -> "int | None":
    """The HTTP status of a `google.api_core.exceptions.GoogleAPICallError`
    (`NotFound.code == 404`, `Forbidden.code == 403`, ...), read from its
    `code` attribute so the SDK need not be importable to classify it. None
    for anything that is not an API error reply."""
    if isinstance(error, OSError):
        return None
    code = getattr(error, "code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _oserror(error: BaseException, path, *, create=False) -> "OSError | None":
    """The OSError an API error reply means for `path`, or None for any
    other exception (a missing SDK, bad credentials, a bug), which must
    propagate as itself rather than read as "no such file". `create`: the
    reply answers a conditional create, where 412 means the object exists."""
    status = _http_status(error)
    if status is None:
        return None
    if status == 404:
        return FileNotFoundError(
            _errno.ENOENT, f"No such file or directory ({error})", str(path)
        )
    if status in (401, 403):
        return PermissionError(_errno.EACCES, str(error), str(path))
    if create and status == 412:
        return FileExistsError(_errno.EEXIST, f"File exists ({error})", str(path))
    return OSError(_errno.EIO, f"GCS request failed: {error}", str(path))


@_contextlib.contextmanager
def _translate_errors(path, *, create=False):
    try:
        yield
    except Exception as error:
        translated = _oserror(error, path, create=create)
        if translated is None:
            raise
        raise translated from error


class _GsWriteStream(_io.BytesIO):
    """Buffers the write and uploads it on close(). `exclusive`
    (`open("x")`) uploads with `if_generation_match=0`: atomic, failing
    with FileExistsError if the object exists. With `initial`
    (`open("r+")`) the buffer starts with the object's content at position
    0 and is uploaded only if it was modified."""

    def __init__(self, path: "GsPath", exclusive=False, initial=None):
        super().__init__(b"" if initial is None else initial)
        self._path = path
        self._exclusive = exclusive
        self._dirty = initial is None

    def write(self, data):
        self._dirty = True
        return super().write(data)

    def writelines(self, lines):
        self._dirty = True
        return super().writelines(lines)

    def truncate(self, size=None):
        self._dirty = True
        return super().truncate(size)

    def close(self):
        if self.closed:
            return
        try:
            if self._dirty:
                self._path._upload(self.getvalue(), exclusive=self._exclusive)
        finally:
            # Closed even when the upload fails, so `IOBase.__del__` does not
            # retry it (over newer content) at garbage collection.
            super().close()


class GsPath(UriPath):
    """`gs:` scheme (`gs://bucket/key/path`): read/write/list via
    `google.cloud.storage`. Requires the `gs` extra. GCS has no real
    directories -- `is_dir()` is prefix emulation (any object key under
    `"<path>/"`), and `mkdir()` creates a zero-byte `"<path>/"` marker
    object; see `docs/divergences.md`. `rename()` uses server-side copy +
    delete (same-bucket only) instead of the generic download+upload+delete
    `move()` fallback; a prefix directory is not renamed
    (NotImplementedError), so `move()` copies and removes it."""

    __SCHEMES = ("gs",)
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseGsBackend

    def _initbackend(self):
        return GsBackend()

    @property
    def bucket_name(self) -> str:
        return self.source.host

    @property
    def key(self) -> str:
        return _object_key(self.path)

    @property
    def _client(self):
        return self.backend.client()

    @property
    def _bucket(self):
        return self._client.bucket(self.bucket_name)

    def _reload(self, key: str):
        """The reloaded blob at `key`, or None if there is no such object.
        Any other failure raises (as OSError when it is an API error)."""
        blob = self._bucket.blob(key)
        try:
            with _translate_errors(self):
                blob.reload()
        except FileNotFoundError:
            return None
        return blob

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        key = self.key
        if key == "":
            # The bucket root: ask the server, as s3: does with HeadBucket,
            # so a mistyped bucket does not read as an existing directory.
            # A one-item listing (NotFound for a missing bucket) needs only
            # the object-list permission the rest of the path uses.
            with _translate_errors(self):
                for _ in self._bucket.list_blobs(max_results=1):
                    break
            return FileStat(is_dir=True)
        # Only a not-found reply falls through to the prefix probe: a
        # transient or permission error read as "missing" let copy() replace
        # an existing object.
        blob = self._reload(key)
        if blob is not None:
            return FileStat(
                st_size=blob.size,
                st_mtime=int(blob.updated.timestamp()) if blob.updated else 0,
                is_dir=False,
            )
        # Not an object at this exact key -- emulate a directory: any
        # object under the "<key>/" prefix means this is a "directory".
        prefix = f"{key}/"
        with _translate_errors(self):
            for _ in self._bucket.list_blobs(prefix=prefix, max_results=1):
                return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _scandir(self):
        # Each list_blobs call already carries size/mtime for every object --
        # reuse it instead of `iterdir()` + a stat call per child.
        prefix = f"{self.key}/" if self.key else ""
        seen = set()
        with _translate_errors(self):
            iterator = self._bucket.list_blobs(prefix=prefix, delimiter="/")
            blobs = list(iterator)
            prefixes = list(iterator.prefixes)
        if not blobs and not prefixes and self.key:
            # Nothing under the prefix: a missing path or a file, which
            # pathlib's iterdir() refuses. Only an empty listing pays this.
            if not self.stat().is_dir():
                raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
        # A key that is both an object and a prefix (`x` and `x/y`) lists as
        # the object, agreeing with stat()'s exact-object precedence; the
        # subtree under it is not listed.
        objects = {blob.name[len(prefix) :] for blob in blobs}
        # Common prefixes (directories)
        for common_prefix in prefixes:
            name = common_prefix[len(prefix) :].rstrip("/")
            if name in objects:
                continue
            if name and name not in seen:
                seen.add(name)
                yield name, FileStat(is_dir=True)
        # Blobs (files)
        for blob in blobs:
            name = blob.name[len(prefix) :]
            if name.endswith("/"):
                continue
            if name and name not in seen:
                seen.add(name)
                mtime = int(blob.updated.timestamp()) if blob.updated else 0
                yield name, FileStat(
                    st_size=blob.size or 0, st_mtime=mtime, is_dir=False
                )

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def _open(self, mode="r", buffering=-1):
        if mode in ("r", "r+"):
            # The client is resolved outside the translation, so a missing
            # SDK raises ImportError instead of FileNotFoundError.
            blob = self._bucket.blob(self.key)
            try:
                with _translate_errors(self):
                    content = blob.download_as_bytes()
            except FileNotFoundError:
                if self.is_dir():
                    raise IsADirectoryError(
                        _errno.EISDIR, "Is a directory", str(self)
                    ) from None
                raise
            if mode == "r+":
                # Read-modify-write: what is written is uploaded on close.
                return _GsWriteStream(self, initial=content)
            # Read-only, like a local file opened "rb".
            return _io.BufferedReader(_io.BytesIO(content))
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _GsWriteStream(self, exclusive=(mode == "x"))

    def _upload(self, data: bytes, *, key=None, exclusive=False) -> None:
        blob = self._bucket.blob(self.key if key is None else key)
        with _translate_errors(self, create=exclusive):
            if exclusive:
                # Generation 0 matches only a missing object: a concurrent
                # creator makes this fail with 412, never overwritten.
                blob.upload_from_string(data, if_generation_match=0)
            else:
                blob.upload_from_string(data)

    def _mkdir(self, mode):
        if self.exists():
            raise FileExistsError(self)
        self._upload(b"", key=f"{self.key}/", exclusive=True)

    def unlink(self, missing_ok=False):
        try:
            st = self.stat()
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if st.is_dir():
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        blob = self._bucket.blob(self.key)
        try:
            with _translate_errors(self):
                blob.delete()
        except FileNotFoundError:
            # Deleted since the stat() above. Anything else -- a hold, a
            # permission error -- is not "already gone" and raises.
            if not missing_ok:
                raise

    def rmdir(self):
        if not self.stat().is_dir():
            raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
        marker = f"{self.key}/"
        with _translate_errors(self):
            for blob in self._bucket.list_blobs(prefix=marker, max_results=2):
                if blob.name != marker:
                    raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
        try:
            with _translate_errors(self):
                self._bucket.blob(marker).delete()
        except FileNotFoundError:
            pass

    def rm(
        self,
        /,
        recursive=False,
        missing_ok=False,
        ignore_error: bool | _ty.Callable[[Exception, _ty.Self], bool] = False,
    ):
        if not recursive:
            return super().rm(
                recursive=recursive,
                missing_ok=missing_ok,
                ignore_error=ignore_error,
            )

        def on_error(error):
            if callable(ignore_error):
                return ignore_error(error, self)
            return bool(ignore_error)

        if not self.key:
            error = PermissionError("recursive bucket delete is not enabled")
            if not on_error(error):
                raise error
            return

        keys = []
        try:
            if self._reload(self.key) is not None:
                keys.append(self.key)
        except OSError as error:
            # Not "missing": deleting the prefix tree instead of the object
            # would remove the wrong thing.
            if not on_error(error):
                raise
            return

        if not keys:
            marker = f"{self.key}/"
            try:
                with _translate_errors(self):
                    keys.extend(
                        blob.name for blob in self._bucket.list_blobs(prefix=marker)
                    )
            except Exception as error:
                if not on_error(error):
                    raise
                return

        if not keys:
            if missing_ok:
                return
            error = FileNotFoundError(self)
            if not on_error(error):
                raise error
            return

        for key in keys:
            try:
                with _translate_errors(self):
                    self._bucket.blob(key).delete()
            except Exception as error:
                if not on_error(error):
                    raise

    def rename(self, target: "GsPath | Uri | str"):
        target = self._rename_target(target)
        dest_key = _object_key(target.path)
        # pathlib returns the new path.
        renamed = self.with_path(target.path)
        if dest_key == self.key:
            # Copying onto itself and then deleting the source loses the object.
            return renamed
        source_blob = self._reload(self.key)
        if source_blob is None:
            # No object at the key: a prefix directory (stat() raises
            # FileNotFoundError when there is nothing at all). move() falls
            # back to copy + rm for it.
            self._pop_stat_hint()
            self.stat()
            raise NotImplementedError(f"rename() of the prefix directory {self}")
        with _translate_errors(self):
            self._bucket.copy_blob(source_blob, self._bucket, dest_key)
            source_blob.delete()
        return renamed
