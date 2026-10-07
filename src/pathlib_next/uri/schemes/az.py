from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import io as _io
import threading as _thread
import typing as _ty

from ... import utils as _utils
from ...path import _check_follow
from ...utils.stat import FileStat
from .. import Uri, UriPath
from . import _objstore as _store


class BaseAzBackend(object):
    """Protocol for obtaining an `azure.storage.blob.BlobServiceClient`.
    Subclass this to plug in custom credential/session handling (e.g. tests
    can override to point at a fake server); `AzBackend` is the real
    implementation."""

    # Weakly referenceable so `UriPath` can record which backends a path
    # derived for itself (see `UriPath._supplied_backend()`).
    __slots__ = ("__weakref__",)

    @_utils.notimplemented
    def client(self): ...


def _default_credential():
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as error:
        raise ImportError(
            "az:// paths without an explicit backend authenticate with "
            "azure-identity's DefaultAzureCredential, which is not installed: "
            "`pip install azure-identity`, or pass "
            "backend=AzBackend(account_url=..., credential=...) or "
            "AzBackend(connection_string=...)"
        ) from error
    return DefaultAzureCredential()


class AzBackend(BaseAzBackend):
    """Lazily creates+caches an `azure.storage.blob.BlobServiceClient`. A
    single client is reused across threads -- it's documented as
    thread-safe.

    `client_kwargs` go to `BlobServiceClient` unchanged; with
    `connection_string`, to `BlobServiceClient.from_connection_string`
    (`credential` and the other options included). `account`, when given
    and neither `account_url` nor `connection_string` is, derives
    `account_url="https://<account>.blob.core.windows.net"`, authenticated
    by azure-identity's `DefaultAzureCredential` unless `credential` is
    passed -- the backend an `AzPath` built without `backend=` uses."""

    __slots__ = ("client_kwargs", "account", "_client")

    def __init__(self, account: "str | None" = None, **client_kwargs):
        self.client_kwargs = client_kwargs
        self.account = account
        self._client = None

    def client(self):
        if self._client is None:
            from azure.storage.blob import BlobServiceClient

            kwargs = dict(self.client_kwargs)
            if "connection_string" in kwargs:
                connection_string = kwargs.pop("connection_string")
                self._client = BlobServiceClient.from_connection_string(
                    connection_string, **kwargs
                )
            else:
                if "account_url" not in kwargs and self.account:
                    kwargs["account_url"] = (
                        f"https://{self.account}.blob.core.windows.net"
                    )
                    if "credential" not in kwargs:
                        kwargs["credential"] = _default_credential()
                self._client = BlobServiceClient(**kwargs)
        return self._client


_DEFAULT_BACKENDS: "dict[str, AzBackend]" = {}
_DEFAULT_BACKENDS_LOCK = _thread.Lock()


def _default_backend(account: str) -> AzBackend:
    """One shared default backend per account, so separately built paths
    reuse one client (and one credential) instead of building their own."""
    with _DEFAULT_BACKENDS_LOCK:
        backend = _DEFAULT_BACKENDS.get(account)
        if backend is None:
            backend = _DEFAULT_BACKENDS[account] = AzBackend(account=account)
        return backend


def _http_status(error: BaseException) -> "int | None":
    """The HTTP status of an `azure.core.exceptions.HttpResponseError`
    (`ResourceNotFoundError`, `ResourceExistsError`, ...), read from its
    `status_code` attribute -- or its type when the SDK set none -- so the
    SDK need not be importable to classify it. None for anything that is
    not an error reply."""
    if isinstance(error, OSError):
        return None
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    try:
        from azure.core import exceptions as _azexc
    except ImportError:
        return None
    if isinstance(error, _azexc.ResourceNotFoundError):
        return 404
    if isinstance(error, _azexc.ResourceExistsError):
        return 409
    if isinstance(error, _azexc.ClientAuthenticationError):
        return 401
    if isinstance(error, _azexc.HttpResponseError):
        return 500
    return None


def _oserror(error: BaseException, path, *, create=False):
    """The OSError an error reply means for `path`, or None for any other
    exception (a missing SDK, bad configuration, a bug), which must
    propagate as itself rather than read as "no such file". `create`: the
    reply answers a conditional create, where 409/412 mean the blob exists
    (elsewhere a 412 is e.g. a lease held by someone else)."""
    status = _http_status(error)
    if status is None:
        return None
    if status == 404:
        return FileNotFoundError(
            _errno.ENOENT, f"No such file or directory ({error})", str(path)
        )
    if status in (401, 403):
        return PermissionError(_errno.EACCES, str(error), str(path))
    if create and status in (409, 412):
        return FileExistsError(_errno.EEXIST, f"File exists ({error})", str(path))
    return OSError(_errno.EIO, f"Azure request failed: {error}", str(path))


@_contextlib.contextmanager
def _translate_errors(path, *, create=False):
    try:
        yield
    except Exception as error:
        translated = _oserror(error, path, create=create)
        if translated is None:
            raise
        raise translated from error


class _AzWriteStream(_io.BytesIO):
    """Buffers the write and uploads it on close(). `exclusive`
    (`open("x")`) uploads with `overwrite=False`: atomic, failing with
    FileExistsError if the blob exists. With `initial` (`open("r+")`) the
    buffer starts with the blob's content at position 0 and is uploaded only
    if it was modified."""

    def __init__(self, path: "AzPath", exclusive=False, initial=None):
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


def _copy_status(props) -> "str | None":
    # `start_copy_from_url()` returns a dict with "copy_status";
    # `get_blob_properties()` returns BlobProperties, whose status lives at
    # `.copy.status` -- indexing it with "copy_status" raised KeyError.
    try:
        return props["copy_status"]
    except (KeyError, TypeError):
        pass
    return getattr(getattr(props, "copy", None), "status", None)


class AzPath(UriPath):
    """`az:` scheme (`az://account/container/key/path`): read/write/list via
    `azure.storage.blob`. Requires the `az` extra. Azure Blob has no real
    directories -- `is_dir()` is prefix emulation (any blob key under
    `"<path>/"`), and `mkdir()` creates a zero-byte `"<path>/"` marker blob;
    see `docs/divergences.md`. `rename()` uses server-side copy + delete
    (same-container only) instead of the generic download+upload+delete
    `move()` fallback; a prefix directory is not renamed
    (NotImplementedError), so `move()` copies and removes it.

    Without `backend=`, the client targets the URI's account
    (`https://<account>.blob.core.windows.net`) with azure-identity's
    `DefaultAzureCredential`; ImportError if azure-identity is missing."""

    __SCHEMES = ("az",)
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseAzBackend

    def _initbackend(self):
        return _default_backend(self.account)

    @property
    def account(self) -> str:
        return self.source.host

    @property
    def container(self) -> str:
        return self.path.lstrip("/").split("/", 1)[0]

    @property
    def key(self) -> str:
        # Everything after the container, minus exactly one trailing "/":
        # `az://acct/cont/dir/` is the directory `dir`, as on s3:/gs:.
        # Interior empty segments (`a//b`) are literal blob-name bytes and
        # stay -- they used to be dropped, making `a//b` unreachable.
        _container, _, key = self.path.lstrip("/").partition("/")
        return key[:-1] if key.endswith("/") else key

    @property
    def _client(self):
        return self.backend.client()

    @property
    def _container(self):
        return self._client.get_container_client(self.container)

    def _properties(self, key: str):
        """The properties of the blob at `key`, or None if there is no such
        blob. Any other failure raises (as OSError when it is an error
        reply)."""
        blob_client = self._container.get_blob_client(key)
        try:
            with _translate_errors(self):
                return blob_client.get_blob_properties()
        except FileNotFoundError:
            return None

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        key = self.key
        if key == "":
            # A container root (or `az://account`, which holds containers):
            # ask the server, as s3: does with HeadBucket, so a mistyped
            # name does not read as an existing directory. A one-item
            # listing needs only the permission the rest of the path uses.
            with _translate_errors(self):
                if self.container:
                    listing = self._container.list_blobs(
                        name_starts_with="", results_per_page=1
                    )
                else:
                    listing = self._client.list_containers(results_per_page=1)
                for _ in listing:
                    break
            return FileStat(is_dir=True)
        # Only a not-found reply falls through to the prefix probe: a
        # transient or permission error read as "missing" let copy() replace
        # an existing blob.
        props = self._properties(key)
        if props is not None:
            return FileStat(
                st_size=props["size"],
                st_mtime=(
                    int(props["last_modified"].timestamp())
                    if props.get("last_modified")
                    else 0
                ),
                is_dir=False,
            )
        # Not a blob at this exact key -- emulate a directory: any blob
        # under the "<key>/" prefix means this is a "directory".
        prefix = f"{key}/"
        with _translate_errors(self):
            for _ in self._container.list_blobs(name_starts_with=prefix):
                return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _scandir(self):
        # Each walk_blobs call already carries size/mtime for every blob --
        # reuse it instead of `iterdir()` + a stat call per child.
        from azure.storage.blob import BlobPrefix

        prefix = f"{self.key}/" if self.key else ""
        seen = set()
        with _translate_errors(self):
            # walk_blobs provides both blobs and common prefixes (directories)
            items = list(
                self._container.walk_blobs(name_starts_with=prefix, delimiter="/")
            )
        if not items and self.key:
            # Nothing under the prefix: a missing path or a file, which
            # pathlib's iterdir() refuses. Only an empty listing pays this.
            if not self.stat().is_dir():
                raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
        # A key that is both a blob and a prefix (`x` and `x/y`) lists as the
        # blob, agreeing with stat()'s exact-blob precedence; the subtree
        # under it is not listed. The SDK returns every prefix of a page
        # before its blobs, so the blobs are collected first.
        blobs = {
            item.name[len(prefix) :]
            for item in items
            if not isinstance(item, BlobPrefix)
        }
        for item in items:
            if isinstance(item, BlobPrefix):
                name = item.name[len(prefix) :].rstrip("/")
                if name in blobs:
                    continue
                # A blob name segment is whatever its writer chose: "..", "."
                # or an embedded "/" must not become a child path.
                if _utils.is_safe_child_name(name) and name not in seen:
                    seen.add(name)
                    yield name, FileStat(is_dir=True)
                continue

            name = item.name[len(prefix) :]
            if _utils.is_safe_child_name(name) and name not in seen:
                seen.add(name)
                mtime = int(item.last_modified.timestamp()) if item.last_modified else 0
                yield name, FileStat(
                    st_size=item.size or 0, st_mtime=mtime, is_dir=False
                )

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def _open(self, mode="r", buffering=-1):
        if mode in ("r", "r+"):
            # The client is resolved outside the translation, so a missing
            # SDK or an unusable default backend raises as itself instead of
            # FileNotFoundError.
            blob_client = self._container.get_blob_client(self.key)
            try:
                with _translate_errors(self):
                    content = blob_client.download_blob().readall()
            except FileNotFoundError:
                if self.is_dir():
                    raise IsADirectoryError(
                        _errno.EISDIR, "Is a directory", str(self)
                    ) from None
                raise
            if mode == "r+":
                # Read-modify-write: what is written is uploaded on close.
                return _AzWriteStream(self, initial=content)
            # Read-only, like a local file opened "rb".
            return _io.BufferedReader(_io.BytesIO(content))
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        if mode == "w" and self._holds_keys(self.key):
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        return _AzWriteStream(self, exclusive=(mode == "x"))

    def _holds_keys(self, key: str) -> bool:
        """Whether any blob lies under the prefix `key/`: a write or rename
        onto it would hide those blobs behind one. Credentials that may write
        but not list cannot ask, and are not stopped."""
        if not key:
            return False
        try:
            with _translate_errors(self):
                for _ in self._container.list_blobs(name_starts_with=f"{key}/"):
                    return True
        except PermissionError:
            pass
        return False

    def _upload(self, data: bytes, *, key=None, exclusive=False) -> None:
        blob_client = self._container.get_blob_client(self.key if key is None else key)
        with _translate_errors(self, create=exclusive):
            # overwrite=False sends If-None-Match: *, so a concurrent creator
            # makes this fail (409), never silently overwritten.
            blob_client.upload_blob(data, overwrite=not exclusive)

    def _mkdir(self, mode):
        # stat(), not exists(): a failed probe must not read as "missing".
        try:
            self.stat()
        except FileNotFoundError:
            pass
        else:
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
        blob_client = self._container.get_blob_client(self.key)
        try:
            with _translate_errors(self):
                blob_client.delete_blob()
        except FileNotFoundError:
            # Deleted since the stat() above. Anything else -- a lease, a
            # permission error -- is not "already gone" and raises.
            if not missing_ok:
                raise

    def rmdir(self):
        if not self.stat().is_dir():
            raise NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))
        if not self.key:
            raise PermissionError(
                _errno.EACCES, "removing a container is not supported", str(self)
            )
        marker = f"{self.key}/"
        count = 0
        with _translate_errors(self):
            for blob in self._container.list_blobs(name_starts_with=marker):
                if blob.name != marker:
                    raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
                count += 1
                if count > 1:
                    break
        marker_blob = self._container.get_blob_client(marker)
        try:
            with _translate_errors(self):
                marker_blob.delete_blob()
        except FileNotFoundError:
            pass

    def rm(
        self,
        /,
        recursive=False,
        missing_ok=False,
        ignore_error: bool | _ty.Callable[[Exception, _ty.Self], bool] = False,
        *,
        follow_symlinks=False,
        follow_binds=False,
    ):
        # An object store holds no symlinks or bindings: the policies are
        # checked and have nothing to decide.
        _check_follow("follow_symlinks", follow_symlinks)
        _check_follow("follow_binds", follow_binds)
        if not recursive:
            return super().rm(
                recursive=recursive,
                missing_ok=missing_ok,
                ignore_error=ignore_error,
                follow_symlinks=follow_symlinks,
                follow_binds=follow_binds,
            )

        def on_error(error):
            if callable(ignore_error):
                return ignore_error(error, self)
            return bool(ignore_error)

        if not self.key:
            error = PermissionError("recursive container delete is not enabled")
            if not on_error(error):
                raise error
            return

        keys = []
        try:
            if self._properties(self.key) is not None:
                keys.append(self.key)
        except OSError as error:
            # Not "missing": deleting the prefix tree instead of the blob
            # would remove the wrong thing.
            if not on_error(error):
                raise
            return

        if not keys:
            marker = f"{self.key}/"
            try:
                entries = self._flat_entries(marker)
            except Exception as error:
                if not on_error(error):
                    raise
                return
            keys = [key for key, _size in entries]
            # What a walk of the directory would not visit is not removed
            # with it: a move copies by walking and then removes this set.
            unreachable = _store.unreachable_keys(marker, entries)
            if unreachable:
                error = _store.refusal(self, "remove", unreachable)
                if not on_error(error):
                    raise error
                skipped = set(unreachable)
                keys = [key for key in keys if key not in skipped]
                if not keys:
                    return

        if not keys:
            if missing_ok:
                return
            error = FileNotFoundError(self)
            if not on_error(error):
                raise error
            return

        try:
            delete_blobs = getattr(self._container, "delete_blobs")
        except AttributeError:
            delete_blobs = None
        if callable(delete_blobs):
            for index in range(0, len(keys), 256):
                batch = keys[index : index + 256]
                try:
                    delete_blobs(*batch)
                except Exception:
                    # A partial failure has already deleted the other keys;
                    # a rejected batch (auth on the batch endpoint, an
                    # emulator without batch support) has deleted none.
                    # Either way every key is retried on its own, so one
                    # failing blob does not leave the rest behind.
                    self._delete_each(batch, on_error, ignore_missing=True)
            return

        self._delete_each(keys, on_error)

    def _flat_entries(self, prefix: str) -> "list[tuple[str, int]]":
        """`(name, size)` of every blob under `prefix`, with no delimiter."""
        with _translate_errors(self):
            return [
                (blob.name, blob.size or 0)
                for blob in self._container.list_blobs(name_starts_with=prefix)
            ]

    def _unreachable_keys(self) -> "list[str]":
        """The names under this prefix directory that a walk does not reach,
        or `[]` for a blob or a missing path."""
        key = self.key
        if key and self._properties(key) is not None:
            return []
        prefix = f"{key}/" if key else ""
        return _store.unreachable_keys(prefix, self._flat_entries(prefix))

    def copy(
        self,
        target,
        *,
        overwrite=False,
        follow_symlinks=True,
        preserve_metadata=True,
        recursive=False,
        ignore_error=None,
        progress=None,
    ):
        # A prefix is copied by walking it; one that holds keys the walk
        # cannot reach is refused up front rather than copied incompletely.
        with _store.checked_copy(self, recursive):
            return super().copy(
                target,
                overwrite=overwrite,
                follow_symlinks=follow_symlinks,
                preserve_metadata=preserve_metadata,
                recursive=recursive,
                ignore_error=ignore_error,
                progress=progress,
            )

    def _delete_each(self, keys, on_error, *, ignore_missing=False):
        for key in keys:
            try:
                with _translate_errors(self):
                    self._container.get_blob_client(key).delete_blob()
            except Exception as error:
                if ignore_missing and isinstance(error, FileNotFoundError):
                    # Removed by the batch that was just retried.
                    continue
                if not on_error(error):
                    raise

    def _same_location(self, other: Uri) -> bool:
        # The authority is the storage account; a rename is a blob copy
        # within one container, so the container must match too.
        if not super()._same_location(other):
            return False
        other_container = next((s for s in other.path.split("/") if s), "")
        return other_container == self.container

    def rename(self, target: "AzPath | Uri | str"):
        target = self._rename_target(target)
        # `with_path`, not `with_segments(target)`: the latter joined the Uri
        # object itself as a segment and raised TypeError for every str target.
        dest = target if isinstance(target, AzPath) else self.with_path(target.path)
        dest_key = dest.key
        if dest_key == self.key:
            # Copying a blob onto itself and then deleting the "source"
            # deletes the only copy. pathlib returns the new path.
            return dest
        if self._properties(self.key) is None:
            # No blob at the key: a prefix directory (stat() raises
            # FileNotFoundError when there is nothing at all). move() falls
            # back to copy + rm for it.
            self._pop_stat_hint()
            self.stat()
            raise NotImplementedError(f"rename() of the prefix directory {self}")
        if self._holds_keys(dest_key):
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(target))
        source_blob_client = self._container.get_blob_client(self.key)
        source_url = source_blob_client.url
        dest_blob_client = self._container.get_blob_client(dest_key)
        with _translate_errors(self):
            # start_copy_from_url is async, poll for completion
            copy_props = dest_blob_client.start_copy_from_url(source_url)
            # Poll until copy is complete
            while _copy_status(copy_props) == "pending":
                import time

                time.sleep(0.1)
                dest_blob_client = self._container.get_blob_client(dest_key)
                copy_props = dest_blob_client.get_blob_properties()
            if _copy_status(copy_props) != "success":
                raise OSError(f"Copy failed: {self} -> {target}")
            source_blob_client.delete_blob()
        return dest
