from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import io as _io
import sys as _sys
import threading as _threading
import typing as _ty

from ... import utils as _utils
from ...utils.stat import FileStat
from . import _objstore as _store
from ._extras import import_client as _import_client


class BaseGsBackend(object):
    """Protocol for obtaining a `google.cloud.storage` Client. Subclass this
    to plug in custom credential/session handling (e.g. tests can override to
    point at a fake server); `GsBackend` is the real implementation."""

    # Weakly referenceable so `UriPath` can record which backends a path
    # derived for itself (see `UriPath._supplied_backend()`).
    __slots__ = ("__weakref__",)

    @_utils.notimplemented
    def client(self):
        """The `google.cloud.storage` client every call of a path goes through."""

    def call_options(self) -> dict:
        """Keyword arguments (`timeout=`, `retry=`) added to every SDK call
        a path makes; none by default, which leaves the SDK's own."""
        return {}


_UNSET = object()


class GsBackend(BaseGsBackend):
    """Lazily creates+caches a `google.cloud.storage` Client. A single client
    is reused across threads -- it's documented as thread-safe.

    `client_kwargs` go to `storage.Client` unchanged, `client_options`
    (dict or `ClientOptions`, `api_endpoint` included) among them. A custom
    endpoint still authenticates; for an emulator or fake server pass
    `use_auth_w_custom_endpoint=False` (anonymous credentials), or set
    `STORAGE_EMULATOR_HOST` yourself -- the SDK reads it.

    `timeout` (seconds, or a `(connect, read)` pair) and `retry` (a
    `google.api_core.retry.Retry`, or None for no retry) are passed to every
    call a path makes; left out, the SDK's own defaults apply, which keep an
    unreachable endpoint waiting for about two minutes."""

    __slots__ = ("client_kwargs", "_client", "_client_lock", "_options")

    def __init__(self, *, timeout=_UNSET, retry=_UNSET, **client_kwargs):
        self.client_kwargs = client_kwargs
        self._client = None
        self._client_lock = _threading.Lock()
        self._options = {
            name: value
            for name, value in (("timeout", timeout), ("retry", retry))
            if value is not _UNSET
        }

    def call_options(self) -> dict:
        return dict(self._options)

    def client(self):
        return _store.lazy_client(self, self._build_client)

    def _build_client(self):
        storage = _import_client("google.cloud.storage", "gs")
        # Passed through as given: turning a dict `api_endpoint` into a
        # process-wide `os.environ["STORAGE_EMULATOR_HOST"]` would redirect
        # every later client in the process, and any subprocess, to it.
        return storage.Client(**self.client_kwargs)

    # A pickled or copied backend keeps its options and builds its own
    # client on first use; the SDK's client refuses to be pickled.
    def __getstate__(self):
        return _store.backend_state(self)

    def __setstate__(self, state):
        _store.restore_backend(self, state)


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


def _is_http_client_error(error: BaseException) -> bool:
    """Whether `error` comes from the HTTP client under the SDK (`requests`
    or `urllib3`), whose exceptions are what a refused connection, a timeout
    or a body cut short look like."""
    return any(
        cls.__module__.split(".")[0] in ("requests", "urllib3")
        for cls in type(error).__mro__
    )


def _transport_kind(error: BaseException) -> str:
    """Which `_store.transport_error()` kind a `requests`/`urllib3` failure is."""
    if _store.mentions_timeout(error):
        return _store.TIMEOUT
    names = {cls.__name__ for cls in type(error).__mro__}
    if names & {"ConnectionError", "NewConnectionError", "ProxyError", "SSLError"}:
        return _store.UNREACHABLE
    if names & {
        "ChunkedEncodingError",
        "ContentDecodingError",
        "ProtocolError",
        "IncompleteRead",
    }:
        return _store.INTERRUPTED
    return _store.FAILED


def _oserror(error: BaseException, path, *, create=False) -> "OSError | None":
    """The OSError an API error reply or a transport failure means for
    `path`, or None for any other exception (a missing SDK, bad credentials,
    a bug), which must propagate as itself rather than read as "no such
    file". `create`: the reply answers a conditional create, where 412 means
    the object exists. A request that ran out of time is TimeoutError, an
    endpoint that cannot be reached ConnectionError. Raise it `from None`."""
    # An error of this type exists only once the module that defines it is
    # loaded, so there is nothing to import here.
    api_exceptions = _sys.modules.get("google.api_core.exceptions")
    if isinstance(error, getattr(api_exceptions, "RetryError", ())):
        # The retry deadline passed: what the last attempt failed with is the
        # reason, and with nothing to say the deadline itself is a timeout.
        translated = (
            None if error.cause is None else _oserror(error.cause, path, create=create)
        )
        return translated or _store.transport_error(_store.TIMEOUT, "GCS", path, error)
    status = _http_status(error)
    if status is None:
        if _is_http_client_error(error):
            return _store.transport_error(_transport_kind(error), "GCS", path, error)
        return None
    if status == 404:
        return FileNotFoundError(
            _errno.ENOENT, f"No such file or directory ({error})", str(path)
        )
    if status in (401, 403):
        return PermissionError(_errno.EACCES, str(error), str(path))
    if create and status == 412:
        return FileExistsError(_errno.EEXIST, f"File exists ({error})", str(path))
    if status in (408, 504):
        return _store.transport_error(_store.TIMEOUT, "GCS", path, error)
    return OSError(_errno.EIO, f"GCS request failed: {error}", str(path))


@_contextlib.contextmanager
def _translate_errors(path, *, create=False):
    try:
        yield
    except Exception as error:
        translated = _oserror(error, path, create=create)
        if translated is None:
            raise
        raise translated from None


_GsWriteStream = _store.BufferedUploadStream


class GsPath(_store.ObjectStorePath):
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
    _LISTING_ERRORS = (Exception,)

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
        if not self.bucket_name:
            raise FileNotFoundError(
                _errno.ENOENT, "a gs: path needs a bucket", str(self)
            )
        return self._client.bucket(self.bucket_name)

    @property
    def _options(self) -> dict:
        """The backend's `timeout=`/`retry=` for an SDK call."""
        options = getattr(self.backend, "call_options", None)
        return options() if callable(options) else {}

    def _reload(self, key: str):
        """The reloaded blob at `key`, or None if there is no such object.
        Any other failure raises (as OSError when it is an API error)."""
        blob = self._bucket.blob(key)
        try:
            with _translate_errors(self):
                blob.reload(**self._options)
        except FileNotFoundError:
            return None
        return blob

    def _scandir(self):
        # Each list_blobs call already carries size/mtime for every object --
        # reuse it instead of `iterdir()` + a stat call per child.
        prefix = f"{self.key}/" if self.key else ""
        seen = set()
        with _translate_errors(self):
            iterator = self._bucket.list_blobs(
                prefix=prefix, delimiter="/", **self._options
            )
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
            # A key segment is whatever its writer chose: "..", "." or an
            # embedded "/" must not become a child path.
            if _utils.is_safe_child_name(name) and name not in seen:
                seen.add(name)
                yield name, FileStat(is_dir=True)
        # Blobs (files)
        for blob in blobs:
            name = blob.name[len(prefix) :]
            if name.endswith("/"):
                continue
            if _utils.is_safe_child_name(name) and name not in seen:
                seen.add(name)
                mtime = int(blob.updated.timestamp()) if blob.updated else 0
                yield name, FileStat(
                    st_size=blob.size or 0, st_mtime=mtime, is_dir=False
                )

    def _open(self, mode="r", buffering=-1):
        if not self.bucket_name:
            raise FileNotFoundError(
                _errno.ENOENT, "a gs: path needs a bucket", str(self)
            )
        if mode in ("r", "r+"):
            if not self.key:
                raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
            # The client is resolved outside the translation, so a missing
            # SDK raises ImportError instead of FileNotFoundError.
            blob = self._bucket.blob(self.key)
            try:
                with _translate_errors(self):
                    content = blob.download_as_bytes(**self._options)
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
        if not self.key:
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        if mode == "w" and self._holds_keys(self.key):
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        return _GsWriteStream(self, exclusive=(mode == "x"))

    def _upload(self, data: bytes, *, key=None, exclusive=False) -> None:
        blob = self._bucket.blob(self.key if key is None else key)
        with _translate_errors(self, create=exclusive):
            if exclusive:
                # Generation 0 matches only a missing object: a concurrent
                # creator makes this fail with 412, never overwritten.
                blob.upload_from_string(data, if_generation_match=0, **self._options)
            else:
                blob.upload_from_string(data, **self._options)

    def _flat_entries(self, prefix: str) -> "list[tuple[str, int]]":
        """`(name, size)` of every object under `prefix`, with no delimiter."""
        with _translate_errors(self):
            return [
                (blob.name, blob.size or 0)
                for blob in self._bucket.list_blobs(prefix=prefix, **self._options)
            ]

    def _stat_root(self) -> FileStat:
        # The bucket root: ask the server, as s3: does with HeadBucket, so a
        # mistyped bucket does not read as an existing directory. A one-item
        # listing (NotFound for a missing bucket) needs only the object-list
        # permission the rest of the path uses.
        with _translate_errors(self):
            for _ in self._bucket.list_blobs(max_results=1, **self._options):
                break
        return FileStat(is_dir=True)

    def _stat_object(self, key: str) -> "FileStat | None":
        # Only a not-found reply means "no such object": a transient or
        # permission error read as "missing" let copy() replace an existing
        # object.
        blob = self._reload(key)
        if blob is None:
            return None
        return FileStat(
            st_size=blob.size,
            st_mtime=int(blob.updated.timestamp()) if blob.updated else 0,
            is_dir=False,
        )

    def _has_object(self, key: str) -> bool:
        return self._reload(key) is not None

    def _has_prefix(self, key: str) -> bool:
        with _translate_errors(self):
            for _ in self._bucket.list_blobs(
                prefix=f"{key}/", max_results=1, **self._options
            ):
                return True
        return False

    def _first_keys(self, prefix: str, limit: int) -> "list[str]":
        with _translate_errors(self):
            return [
                blob.name
                for blob in self._bucket.list_blobs(
                    prefix=prefix, max_results=limit, **self._options
                )
            ]

    def _put_marker(self, key: str) -> None:
        self._upload(b"", key=key, exclusive=True)

    def _delete_object(self, key: str, *, missing_ok: bool) -> None:
        blob = self._bucket.blob(key)
        try:
            with _translate_errors(self):
                blob.delete(**self._options)
        except FileNotFoundError:
            # Deleted since the caller looked. Anything else -- a hold, a
            # permission error -- is not "already gone" and raises.
            if not missing_ok:
                raise

    def _delete_keys(self, keys: "list[str]", on_error) -> None:
        for key in keys:
            try:
                with _translate_errors(self._key_path(key)):
                    self._bucket.blob(key).delete(**self._options)
            except Exception as error:
                if not on_error(error, self._key_path(key)):
                    raise

    def _rename_source(self, key: str):
        return self._reload(key)

    def _rename_copy(self, source_blob, dest, dest_key: str) -> None:
        with _translate_errors(self):
            self._bucket.copy_blob(source_blob, self._bucket, dest_key, **self._options)
            source_blob.delete(**self._options)
