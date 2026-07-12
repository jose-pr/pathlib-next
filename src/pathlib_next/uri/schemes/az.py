from __future__ import annotations

import io as _io
import typing as _ty

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import Source, Uri, UriPath


class BaseAzBackend(object):
    """Protocol for obtaining an `azure.storage.blob.BlobServiceClient`.
    Subclass this to plug in custom credential/session handling (e.g. tests
    can override to point at a fake server); `AzBackend` is the real
    implementation."""

    __slots__ = ()

    @_utils.notimplemented
    def client(self): ...


class AzBackend(BaseAzBackend):
    """Lazily creates+caches an `azure.storage.blob.BlobServiceClient`. A
    single client is reused across threads -- it's documented as
    thread-safe."""

    __slots__ = ("client_kwargs", "_client")

    def __init__(self, **client_kwargs):
        self.client_kwargs = client_kwargs
        self._client = None

    def client(self):
        if self._client is None:
            from azure.storage.blob import BlobServiceClient

            kwargs = dict(self.client_kwargs)
            # Handle connection_string directly if provided
            if "connection_string" in kwargs:
                self._client = BlobServiceClient.from_connection_string(
                    kwargs["connection_string"]
                )
            else:
                self._client = BlobServiceClient(**kwargs)
        return self._client


class _AzWriteStream(_io.BytesIO):
    def __init__(self, path: "AzPath"):
        super().__init__()
        self._path = path

    def close(self):
        if not self.closed:
            container = self._path._container
            blob_client = container.get_blob_client(self._path.key)
            blob_client.upload_blob(self.getvalue(), overwrite=True)
        super().close()


class AzPath(UriPath):
    """`az:` scheme (`az://account/container/key/path`): read/write/list via
    `azure.storage.blob`. Requires the `az` extra. Azure Blob has no real
    directories -- `is_dir()` is prefix emulation (any blob key under
    `"<path>/"`), and `mkdir()` creates a zero-byte `"<path>/"` marker blob;
    see `docs/divergences.md`. `rename()` uses server-side copy + delete
    (same-container only) instead of the generic download+upload+delete
    `move()` fallback."""

    __SCHEMES = ("az",)
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseAzBackend

    def _initbackend(self):
        return AzBackend()

    @property
    def account(self) -> str:
        return self.source.host

    @property
    def container(self) -> str:
        segments = self.segments
        # Skip leading empty string, get the first real segment
        real_segments = [s for s in segments if s]
        return real_segments[0] if real_segments else ""

    @property
    def key(self) -> str:
        segments = self.segments
        # Skip leading empty string and first real segment (container)
        real_segments = [s for s in segments if s]
        return "/".join(real_segments[1:]) if len(real_segments) > 1 else ""

    @property
    def _client(self):
        return self.backend.client()

    @property
    def _container(self):
        return self._client.get_container_client(self.container)

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        key = self.key
        if key == "":
            # Root is always a container, which always exists
            return FileStat(is_dir=True)
        try:
            blob_client = self._container.get_blob_client(key)
            props = blob_client.get_blob_properties()
            return FileStat(
                st_size=props["size"],
                st_mtime=int(props["last_modified"].timestamp()) if props.get("last_modified") else 0,
                is_dir=False,
            )
        except Exception:
            pass
        # Not a blob at this exact key -- emulate a directory: any blob
        # under the "<key>/" prefix means this is a "directory".
        prefix = f"{key}/"
        for _ in self._container.list_blobs(name_starts_with=prefix):
            return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _scandir(self):
        # Each walk_blobs call already carries size/mtime for every blob --
        # reuse it instead of `iterdir()` + a stat call per child.
        from azure.storage.blob import BlobPrefix

        prefix = f"{self.key}/" if self.key else ""
        seen = set()
        # walk_blobs provides both blobs and common prefixes (directories)
        for item in self._container.walk_blobs(name_starts_with=prefix, delimiter="/"):
            if isinstance(item, BlobPrefix):
                name = item.name[len(prefix) :].rstrip("/")
                if name and name not in seen:
                    seen.add(name)
                    yield name, FileStat(is_dir=True)
                continue

            name = item.name[len(prefix) :]
            if name and name not in seen:
                seen.add(name)
                mtime = int(item.last_modified.timestamp()) if item.last_modified else 0
                yield name, FileStat(
                    st_size=item.size or 0, st_mtime=mtime, is_dir=False
                )

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def _open(self, mode="r", buffering=-1):
        if "r" in mode:
            try:
                blob_client = self._container.get_blob_client(self.key)
                content = blob_client.download_blob().readall()
            except Exception as error:
                raise FileNotFoundError(self) from error
            return _io.BytesIO(content)
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _AzWriteStream(self)

    def _mkdir(self, mode):
        if self.exists():
            raise FileExistsError(self)
        blob_client = self._container.get_blob_client(f"{self.key}/")
        blob_client.upload_blob(b"")

    def unlink(self, missing_ok=False):
        if not missing_ok and not self.exists():
            raise FileNotFoundError(self)
        blob_client = self._container.get_blob_client(self.key)
        try:
            blob_client.delete_blob()
        except Exception as error:
            if missing_ok:
                return
            raise FileNotFoundError(self) from error

    def rmdir(self):
        marker = f"{self.key}/"
        count = 0
        for blob in self._container.list_blobs(name_starts_with=marker):
            if blob.name != marker:
                raise OSError(f"Directory not empty: {self}")
            count += 1
            if count > 1:
                break
        marker_blob = self._container.get_blob_client(marker)
        marker_blob.delete_blob()

    def rename(self, target: "AzPath | Uri | str"):
        if not isinstance(target, Uri):
            target = Uri(self.parent, target)
        dest_key = self.with_segments(target).key if not isinstance(target, AzPath) else target.key
        source_blob_client = self._container.get_blob_client(self.key)
        source_url = source_blob_client.url
        dest_blob_client = self._container.get_blob_client(dest_key)
        # start_copy_from_url is async, poll for completion
        copy_props = dest_blob_client.start_copy_from_url(source_url)
        # Poll until copy is complete
        while copy_props["copy_status"] == "pending":
            import time
            time.sleep(0.1)
            dest_blob_client = self._container.get_blob_client(dest_key)
            copy_props = dest_blob_client.get_blob_properties()
        if copy_props["copy_status"] != "success":
            raise OSError(f"Copy failed: {self} -> {target}")
        source_blob_client.delete_blob()
