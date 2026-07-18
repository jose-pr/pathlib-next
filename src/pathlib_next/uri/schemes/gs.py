from __future__ import annotations

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
    is reused across threads -- it's documented as thread-safe."""

    __slots__ = ("client_kwargs", "_client")

    def __init__(self, **client_kwargs):
        self.client_kwargs = client_kwargs
        self._client = None

    def client(self):
        if self._client is None:
            import os
            from google.cloud import storage

            kwargs = dict(self.client_kwargs)
            # Handle api_endpoint: set env var for google-cloud-storage emulator support
            if "client_options" in kwargs and isinstance(kwargs["client_options"], dict):
                endpoint = kwargs["client_options"].get("api_endpoint")
                if endpoint:
                    os.environ["STORAGE_EMULATOR_HOST"] = endpoint
                    del kwargs["client_options"]
            self._client = storage.Client(**kwargs)
        return self._client


class _GsWriteStream(_io.BytesIO):
    def __init__(self, path: "GsPath"):
        super().__init__()
        self._path = path

    def close(self):
        if not self.closed:
            bucket = self._path._bucket
            blob = bucket.blob(self._path.key)
            blob.upload_from_string(self.getvalue())
        super().close()


class GsPath(UriPath):
    """`gs:` scheme (`gs://bucket/key/path`): read/write/list via
    `google.cloud.storage`. Requires the `gs` extra. GCS has no real
    directories -- `is_dir()` is prefix emulation (any object key under
    `"<path>/"`), and `mkdir()` creates a zero-byte `"<path>/"` marker
    object; see `docs/divergences.md`. `rename()` uses server-side copy +
    delete (same-bucket only) instead of the generic download+upload+delete
    `move()` fallback."""

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
        return self.path.lstrip("/")

    @property
    def _client(self):
        return self.backend.client()

    @property
    def _bucket(self):
        return self._client.bucket(self.bucket_name)

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        key = self.key
        if key == "":
            # Root bucket always exists - don't try to reload
            return FileStat(is_dir=True)
        try:
            blob = self._bucket.blob(key)
            blob.reload()
            return FileStat(
                st_size=blob.size,
                st_mtime=int(blob.updated.timestamp()) if blob.updated else 0,
                is_dir=False,
            )
        except Exception:
            pass
        # Not an object at this exact key -- emulate a directory: any
        # object under the "<key>/" prefix means this is a "directory".
        prefix = f"{key}/"
        for _ in self._bucket.list_blobs(prefix=prefix, max_results=1):
            return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _scandir(self):
        # Each list_blobs call already carries size/mtime for every object --
        # reuse it instead of `iterdir()` + a stat call per child.
        prefix = f"{self.key}/" if self.key else ""
        seen = set()
        iterator = self._bucket.list_blobs(prefix=prefix, delimiter="/")
        blobs = list(iterator)
        # Common prefixes (directories)
        for common_prefix in iterator.prefixes:
            name = common_prefix[len(prefix) :].rstrip("/")
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
        if "r" in mode:
            try:
                blob = self._bucket.blob(self.key)
                content = blob.download_as_bytes()
            except Exception as error:
                raise FileNotFoundError(self) from error
            return _io.BytesIO(content)
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _GsWriteStream(self)

    def _mkdir(self, mode):
        if self.exists():
            raise FileExistsError(self)
        blob = self._bucket.blob(f"{self.key}/")
        blob.upload_from_string(b"")

    def unlink(self, missing_ok=False):
        if not missing_ok and not self.exists():
            raise FileNotFoundError(self)
        blob = self._bucket.blob(self.key)
        try:
            blob.delete()
        except Exception as error:
            if missing_ok:
                return
            raise FileNotFoundError(self) from error

    def rmdir(self):
        marker = f"{self.key}/"
        for blob in self._bucket.list_blobs(prefix=marker, max_results=2):
            if blob.name != marker:
                raise OSError(f"Directory not empty: {self}")
        marker_blob = self._bucket.blob(marker)
        marker_blob.delete()

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
            blob = self._bucket.blob(self.key)
            blob.reload()
            keys.append(self.key)
        except Exception:
            keys = []

        if not keys:
            marker = f"{self.key}/"
            try:
                keys.extend(blob.name for blob in self._bucket.list_blobs(prefix=marker))
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
                self._bucket.blob(key).delete()
            except Exception as error:
                if not on_error(error):
                    raise

    def rename(self, target: "GsPath | Uri | str"):
        if not isinstance(target, Uri):
            target = Uri(self.parent, target)
        dest_key = target.path.lstrip("/")
        source_blob = self._bucket.blob(self.key)
        self._bucket.copy_blob(source_blob, self._bucket, dest_key)
        source_blob.delete()
