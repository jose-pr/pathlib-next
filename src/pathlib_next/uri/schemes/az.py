from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import io as _io
import itertools as _itertools
import sys as _sys
import threading as _thread
import time as _time
import typing as _ty

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import Uri
from . import _objstore as _store
from ._extras import import_client as _import_client


class BaseAzBackend(object):
    """Protocol for obtaining an `azure.storage.blob.BlobServiceClient`.
    Subclass this to plug in custom credential/session handling (e.g. tests
    can override to point at a fake server); `AzBackend` is the real
    implementation."""

    # Weakly referenceable so `UriPath` can record which backends a path
    # derived for itself (see `UriPath._supplied_backend()`).
    __slots__ = ("__weakref__",)

    @_utils.notimplemented
    def client(self):
        """The `BlobServiceClient` every call of a path goes through."""


def _default_credential():
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as error:
        raise ImportError(
            "az:// paths without an explicit backend authenticate with "
            "azure-identity's DefaultAzureCredential, which is not installed: "
            'pip install "pathlib-next[az]" (the az extra), or pass '
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

    __slots__ = ("client_kwargs", "account", "_client", "_client_lock")

    def __init__(self, account: "str | None" = None, **client_kwargs):
        self.client_kwargs = client_kwargs
        self.account = account
        self._client = None
        self._client_lock = _thread.Lock()

    def client(self):
        return _store.lazy_client(self, self._build_client)

    def _build_client(self):
        BlobServiceClient = _import_client("azure.storage.blob", "az").BlobServiceClient

        kwargs = dict(self.client_kwargs)
        if "connection_string" in kwargs:
            connection_string = kwargs.pop("connection_string")
            return BlobServiceClient.from_connection_string(connection_string, **kwargs)
        if "account_url" not in kwargs and self.account:
            kwargs["account_url"] = f"https://{self.account}.blob.core.windows.net"
            if "credential" not in kwargs:
                kwargs["credential"] = _default_credential()
        return BlobServiceClient(**kwargs)

    # A pickled or copied backend keeps its options and builds its own
    # client (and credential) on first use.
    def __getstate__(self):
        return _store.backend_state(self)

    def __setstate__(self, state):
        _store.restore_backend(self, state)


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


def _transport_kind(error: BaseException) -> "str | None":
    """Which `_store.transport_error()` kind `error` is when it is the SDK's
    report of a connection failure (not of a reply), else None."""
    # An SDK error exists only once its module is loaded, so there is nothing
    # to import here.
    azexc = _sys.modules.get("azure.core.exceptions")
    if azexc is None:
        return None
    timeouts = tuple(
        getattr(azexc, name)
        for name in ("ServiceRequestTimeoutError", "ServiceResponseTimeoutError")
        if hasattr(azexc, name)
    )
    if isinstance(error, timeouts):
        return _store.TIMEOUT
    if isinstance(error, azexc.ServiceRequestError):
        # The request could not be sent: no connection, no name, no route.
        return _store.TIMEOUT if _store.mentions_timeout(error) else _store.UNREACHABLE
    if isinstance(error, azexc.ServiceResponseError):
        # Sent, but the answer did not arrive whole.
        return _store.TIMEOUT if _store.mentions_timeout(error) else _store.INTERRUPTED
    incomplete = getattr(azexc, "IncompleteReadError", None)
    if incomplete is not None and isinstance(error, incomplete):
        return _store.INTERRUPTED
    return None


def _oserror(error: BaseException, path, *, create=False):
    """The OSError an error reply or a connection failure means for `path`,
    or None for any other exception (a missing SDK, bad configuration, a
    bug), which must propagate as itself rather than read as "no such
    file". `create`: the reply answers a conditional create, where 409/412
    mean the blob exists (elsewhere a 412 is e.g. a lease held by someone
    else). A request that ran out of time is TimeoutError, an endpoint that
    cannot be reached ConnectionError. Raise it `from None`."""
    kind = _transport_kind(error)
    if kind is not None:
        return _store.transport_error(kind, "Azure", path, error)
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
    if status in (408, 504):
        return _store.transport_error(_store.TIMEOUT, "Azure", path, error)
    return OSError(_errno.EIO, f"Azure request failed: {error}", str(path))


@_contextlib.contextmanager
def _translate_errors(path, *, create=False):
    try:
        yield
    except Exception as error:
        translated = _oserror(error, path, create=create)
        if translated is None:
            raise
        raise translated from None


_AzWriteStream = _store.BufferedUploadStream


COPY_POLL_TIMEOUT = 300.0
"""Seconds `AzPath.rename()` waits for the server-side copy to finish. A copy
inside one account is normally done when it is started; past this the copy is
aborted (the destination may still appear if the abort fails) and `rename()`
raises `TimeoutError`, the source untouched."""

_COPY_POLL_INTERVAL = 0.1


def _copy_id(props) -> "str | None":
    try:
        return props["copy_id"]
    except (KeyError, TypeError):
        pass
    return getattr(getattr(props, "copy", None), "id", None)


def _copy_status(props) -> "str | None":
    # `start_copy_from_url()` returns a dict with "copy_status";
    # `get_blob_properties()` returns BlobProperties, whose status lives at
    # `.copy.status` -- indexing it with "copy_status" raised KeyError.
    try:
        return props["copy_status"]
    except (KeyError, TypeError):
        pass
    return getattr(getattr(props, "copy", None), "status", None)


class AzPath(_store.ObjectStorePath):
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
    _TOP = "container"
    _LISTING_ERRORS = (Exception,)

    if _ty.TYPE_CHECKING:
        backend: BaseAzBackend

    def _initbackend(self):
        self._check_account()
        return _default_backend(self.account)

    def _check_account(self) -> None:
        """FileNotFoundError for a path with no account whose backend is the
        default one; a backend the caller supplied names its own account."""
        if not self.account and self._supplied_backend() is None:
            raise FileNotFoundError(
                _errno.ENOENT, "an az: path needs a storage account", str(self)
            )

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
        # stay, so `a//b` is addressable.
        _container, _, key = self.path.lstrip("/").partition("/")
        return key[:-1] if key.endswith("/") else key

    @property
    def _client(self):
        return self.backend.client()

    @property
    def _container(self):
        return self._client.get_container_client(self.container)

    def _key_path(self, key: str) -> "AzPath":
        """The path of the blob `key` in this path's container."""
        return self.with_path(f"/{self.container}/{key}")

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

    def _scandir(self):
        # Each walk_blobs call already carries size/mtime for every blob --
        # reuse it instead of `iterdir()` + a stat call per child.
        BlobPrefix = _import_client("azure.storage.blob", "az").BlobPrefix

        if not self.container:
            # The account: its containers are its directories.
            with _translate_errors(self):
                names = [item.name for item in self._client.list_containers()]
            for name in names:
                if _utils.is_safe_child_name(name):
                    yield name, FileStat(is_dir=True)
            return
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

    def _open(self, mode="r", buffering=-1):
        self._check_account()
        if mode in ("r", "r+"):
            if not self.key:
                # An account or a container, not a blob.
                raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
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
        if not self.key:
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        if mode == "w" and self._holds_keys(self.key):
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        return _AzWriteStream(self, exclusive=(mode == "x"))

    def _upload(self, data: bytes, *, key=None, exclusive=False) -> None:
        blob_client = self._container.get_blob_client(self.key if key is None else key)
        with _translate_errors(self, create=exclusive):
            # overwrite=False sends If-None-Match: *, so a concurrent creator
            # makes this fail (409), never silently overwritten.
            blob_client.upload_blob(data, overwrite=not exclusive)

    def _flat_entries(self, prefix: str) -> "list[tuple[str, int]]":
        """`(name, size)` of every blob under `prefix`, with no delimiter."""
        with _translate_errors(self):
            return [
                (blob.name, blob.size or 0)
                for blob in self._container.list_blobs(name_starts_with=prefix)
            ]

    def _delete_each(self, keys, on_error, *, ignore_missing=False):
        for key in keys:
            try:
                with _translate_errors(self._key_path(key)):
                    self._container.get_blob_client(key).delete_blob()
            except Exception as error:
                if ignore_missing and isinstance(error, FileNotFoundError):
                    # Removed by the batch that was just retried.
                    continue
                if not on_error(error, self._key_path(key)):
                    raise

    def _same_location(self, other: Uri) -> bool:
        # The authority is the storage account; a rename is a blob copy
        # within one container, so the container must match too.
        if not super()._same_location(other):
            return False
        other_container = next((s for s in other.path.split("/") if s), "")
        return other_container == self.container

    @staticmethod
    def _abort_copy(blob_client, copy_id) -> None:
        """Stop a copy that is still pending; best effort."""
        if copy_id is not None:
            try:
                blob_client.abort_copy(copy_id)
            except Exception:
                pass

    def _stat_root(self) -> FileStat:
        # A container root (or `az://account`, which holds containers): ask
        # the server, as s3: does with HeadBucket, so a mistyped name does
        # not read as an existing directory. A one-item listing needs only
        # the permission the rest of the path uses.
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

    def _stat_object(self, key: str) -> "FileStat | None":
        # Only a not-found reply means "no such blob": a transient or
        # permission error read as "missing" let copy() replace an existing
        # blob.
        props = self._properties(key)
        if props is None:
            return None
        return FileStat(
            st_size=props["size"],
            st_mtime=(
                int(props["last_modified"].timestamp())
                if props.get("last_modified")
                else 0
            ),
            is_dir=False,
        )

    def _has_object(self, key: str) -> bool:
        return self._properties(key) is not None

    def _has_prefix(self, key: str) -> bool:
        with _translate_errors(self):
            # One item answers; the default page would carry up to 5000.
            for _ in self._container.list_blobs(
                name_starts_with=f"{key}/", results_per_page=1
            ):
                return True
        return False

    def _first_keys(self, prefix: str, limit: int) -> "list[str]":
        with _translate_errors(self):
            return [
                blob.name
                for blob in _itertools.islice(
                    self._container.list_blobs(
                        name_starts_with=prefix, results_per_page=limit
                    ),
                    limit,
                )
            ]

    def _put_marker(self, key: str) -> None:
        self._upload(b"", key=key, exclusive=True)

    def _delete_object(self, key: str, *, missing_ok: bool) -> None:
        blob_client = self._container.get_blob_client(key)
        try:
            with _translate_errors(self):
                blob_client.delete_blob()
        except FileNotFoundError:
            # Deleted since the caller looked. Anything else -- a lease, a
            # permission error -- is not "already gone" and raises.
            if not missing_ok:
                raise

    def _delete_keys(self, keys: "list[str]", on_error) -> None:
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

    def _rename_dest(self, target) -> "AzPath":
        # `with_path`, not `with_segments(target)`: the latter joined the Uri
        # object itself as a segment and raised TypeError for every str target.
        return target if isinstance(target, AzPath) else self.with_path(target.path)

    def _rename_source(self, key: str):
        return self._properties(key)

    def _rename_copy(self, props, dest: "AzPath", dest_key: str) -> None:
        source_blob_client = self._container.get_blob_client(self.key)
        source_url = source_blob_client.url
        dest_blob_client = self._container.get_blob_client(dest_key)
        with _translate_errors(self):
            # start_copy_from_url is async, poll for completion
            copy_props = dest_blob_client.start_copy_from_url(source_url)
            copy_id = _copy_id(copy_props)
            # Poll until copy is complete
            deadline = _time.monotonic() + COPY_POLL_TIMEOUT
            while _copy_status(copy_props) == "pending":
                if _time.monotonic() >= deadline:
                    self._abort_copy(dest_blob_client, copy_id)
                    raise TimeoutError(
                        _errno.ETIMEDOUT,
                        f"the copy did not finish within {COPY_POLL_TIMEOUT:g} s",
                        str(self),
                    )
                _time.sleep(_COPY_POLL_INTERVAL)
                dest_blob_client = self._container.get_blob_client(dest_key)
                copy_props = dest_blob_client.get_blob_properties()
            if _copy_status(copy_props) != "success":
                raise OSError(f"Copy failed: {self} -> {dest}")
            source_blob_client.delete_blob()
