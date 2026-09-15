from __future__ import annotations

import errno as _errno
import io as _io
import itertools as _itertools
import tempfile as _tempfile
import typing as _ty

import botocore.exceptions as _botoexc

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import Uri, UriPath

try:
    from boto3.exceptions import S3UploadFailedError as _S3UploadFailedError
except ImportError:  # a botocore-only BaseS3Backend
    _S3UploadFailedError = ()


class BaseS3Backend(object):
    """Protocol for obtaining a `boto3` S3 client. Subclass this to plug in
    custom credential/session handling (e.g. tests mock it directly, no
    real AWS account needed); `S3Backend` is the real implementation."""

    __slots__ = ()

    @_utils.notimplemented
    def client(self): ...


class S3Backend(BaseS3Backend):
    """Lazily creates+caches a `boto3` S3 client. Unlike `sftp.py`'s/
    `ftp.py`'s per-thread connection pools, a single `boto3` client is
    reused across threads -- it's documented as thread-safe."""

    __slots__ = ("client_kwargs", "_client")

    def __init__(self, **client_kwargs):
        self.client_kwargs = client_kwargs
        self._client = None

    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", **self.client_kwargs)
        return self._client


_PUT_OBJECT_LIMIT = 5 * 1024**3
"""Largest body a single PutObject accepts, and the largest source a single
CopyObject copies; above it writes and renames use boto3's managed
(multipart) transfers."""

_SPOOL_SIZE = 8 * 1024**2
"""Bytes a write stream keeps in memory before spilling to a temp file."""

# botocore's connection-level failures: an endpoint that cannot be reached,
# a timeout, a dropped connection. Not an OSError, so exists() and walk()'s
# on_error would not see them.
_TRANSPORT_ERRORS = (_botoexc.ConnectionError, _botoexc.HTTPClientError)
_S3_ERRORS = (_botoexc.ClientError,) + _TRANSPORT_ERRORS


def _error_code(error: _botoexc.ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _http_status(error: _botoexc.ClientError) -> "int | None":
    return error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")


def _is_not_found(error: _botoexc.ClientError) -> bool:
    return (
        _error_code(error) in ("404", "NoSuchKey", "NoSuchBucket", "NotFound")
        or _http_status(error) == 404
    )


def _is_precondition_failed(error: _botoexc.ClientError) -> bool:
    # 412 PreconditionFailed: the key exists. 409 ConditionalRequestConflict:
    # a concurrent conditional write to the key is in progress.
    return (
        _error_code(error) in ("PreconditionFailed", "ConditionalRequestConflict")
        or _http_status(error) == 412
    )


def _is_not_implemented(error: _botoexc.ClientError) -> bool:
    return _error_code(error) == "NotImplemented" or _http_status(error) == 501


def _oserror(error: Exception, path) -> OSError:
    """The OSError a botocore error means for `path`: not found (a key or a
    bucket) is FileNotFoundError, access denied PermissionError, anything
    else OSError. Raise it `from error` to keep the SDK error."""
    if not isinstance(error, _botoexc.ClientError):
        return OSError(_errno.EIO, f"S3 request failed: {error}", str(path))
    code = _error_code(error)
    message = error.response.get("Error", {}).get("Message") or str(error)
    if _is_not_found(error):
        return FileNotFoundError(
            _errno.ENOENT, f"No such file or directory ({code})", str(path)
        )
    if code in ("403", "401", "AccessDenied", "AllAccessDisabled") or (
        _http_status(error) in (401, 403)
    ):
        return PermissionError(_errno.EACCES, f"{code}: {message}", str(path))
    return OSError(_errno.EIO, f"S3 {code}: {message}", str(path))


def _object_key(path: str) -> str:
    """The object key a s3 URI path names: leading `/`s dropped, and
    exactly one trailing `/`. `s3://b/dir/` is the directory `dir`, the
    way pathlib drops a trailing slash -- keeping it made the key `dir/`, so
    the `dir/` marker object read as a file and `rm(recursive=True)` deleted
    only the marker. Interior empty segments (`a//b`) are literal key bytes
    and stay."""
    key = path.lstrip("/")
    return key[:-1] if key.endswith("/") else key


class _S3BodyReader(_io.RawIOBase):
    """Reads a GetObject `StreamingBody` as it arrives, instead of loading
    the whole object into memory first."""

    def __init__(self, body):
        self._body = body

    def readable(self):
        return True

    def readinto(self, buffer):
        data = self._body.read(len(buffer))
        size = len(data)
        buffer[:size] = data
        return size

    def close(self):
        if self.closed:
            return
        try:
            self._body.close()
        finally:
            super().close()


class _S3WriteStream(_io.BufferedIOBase):
    """Spools the write (in memory up to `_SPOOL_SIZE`, then a temp file)
    and uploads it on close(): a managed transfer, multipart above boto3's
    threshold, so objects over PutObject's 5 GB limit can be written.

    `exclusive` (`open("x")`) creates the key with a conditional PutObject
    (`IfNoneMatch="*"`), atomic against a concurrent creator. With
    `initial` (`open("r+")`) the spool starts with the object's content at
    position 0 and is uploaded only if it was modified."""

    def __init__(self, path: "S3Path", exclusive=False, initial=None):
        super().__init__()
        self._path = path
        self._exclusive = exclusive
        self._file = _tempfile.SpooledTemporaryFile(max_size=_SPOOL_SIZE)
        self._dirty = initial is None
        if initial is not None:
            while True:
                chunk = initial.read(_io.DEFAULT_BUFFER_SIZE)
                if not chunk:
                    break
                self._file.write(chunk)
            self._file.seek(0)

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def _check_open(self):
        if self.closed:
            raise ValueError("I/O operation on closed file.")

    def write(self, data):
        self._check_open()
        self._dirty = True
        return self._file.write(data)

    def read(self, size=-1):
        self._check_open()
        return self._file.read(size)

    def read1(self, size=-1):
        return self.read(size)

    def seek(self, offset, whence=_io.SEEK_SET):
        self._check_open()
        self._file.seek(offset, whence)
        return self._file.tell()

    def tell(self):
        self._check_open()
        return self._file.tell()

    def truncate(self, size=None):
        self._check_open()
        self._dirty = True
        if size is None:
            size = self._file.tell()
        return self._file.truncate(size)

    def close(self):
        if self.closed:
            return
        try:
            if self._dirty:
                self._upload()
        finally:
            # Closed even when the upload fails, so `IOBase.__del__` does not
            # retry it (over newer content) at garbage collection.
            try:
                self._file.close()
            finally:
                super().close()

    def _upload(self):
        path = self._path
        client = path._client
        body = self._file
        body.seek(0, _io.SEEK_END)
        size = body.tell()
        body.seek(0)
        if self._exclusive:
            if size <= _PUT_OBJECT_LIMIT and path._put_exclusive(body):
                return
            # Over PutObject's limit (managed uploads take no IfNoneMatch),
            # or a store without conditional writes: only this check guards
            # the key.
            if path.exists():
                raise FileExistsError(_errno.EEXIST, "File exists", str(path))
            body.seek(0)
        try:
            upload_fileobj = getattr(client, "upload_fileobj", None)
            if upload_fileobj is None:
                # A plain botocore client (no boto3 transfer methods).
                client.put_object(Bucket=path.bucket, Key=path.key, Body=body)
            else:
                upload_fileobj(body, path.bucket, path.key)
        except _S3_ERRORS as error:
            raise _oserror(error, path) from error
        except _S3UploadFailedError as error:
            raise OSError(_errno.EIO, str(error), str(path)) from error


class S3Path(UriPath):
    """`s3:` scheme (`s3://bucket/key/path`): read/write/list via `boto3`.
    Requires the `s3` extra. S3 has no real directories -- `is_dir()` is
    prefix emulation (any object key under `"<path>/"`), and `mkdir()`
    creates a zero-byte `"<path>/"` marker object (the same convention the
    AWS console itself uses for an empty "folder"); see
    `docs/divergences.md`. `rename()` uses server-side `copy_object` +
    `delete_object` (same-bucket only) instead of the generic
    download+upload+delete `move()` fallback; a prefix directory is not
    renamed (NotImplementedError), so `move()` copies and removes it."""

    __SCHEMES = ("s3",)
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseS3Backend

    def _initbackend(self):
        return S3Backend()

    @property
    def bucket(self) -> str:
        return self.source.host

    @property
    def key(self) -> str:
        return _object_key(self.path)

    @property
    def _client(self):
        return self.backend.client()

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        key = self.key
        client = self._client
        if key == "":
            try:
                client.head_bucket(Bucket=self.bucket)
            except _S3_ERRORS as error:
                raise _oserror(error, self) from error
            return FileStat(is_dir=True)
        try:
            head = client.head_object(Bucket=self.bucket, Key=key)
        except _botoexc.ClientError as error:
            # A 403 stays PermissionError: S3 answers HEAD of a missing key
            # with 403 when the caller may not list the bucket.
            if not _is_not_found(error):
                raise _oserror(error, self) from error
        except _TRANSPORT_ERRORS as error:
            raise _oserror(error, self) from error
        else:
            return FileStat(
                st_size=head["ContentLength"],
                st_mtime=int(head["LastModified"].timestamp()),
                is_dir=False,
            )
        # Not an object at this exact key -- emulate a directory: any
        # object under the "<key>/" prefix means this is a "directory".
        try:
            resp = client.list_objects_v2(
                Bucket=self.bucket, Prefix=f"{key}/", MaxKeys=1
            )
        except _S3_ERRORS as error:
            raise _oserror(error, self) from error
        if resp.get("KeyCount", 0) > 0:
            return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _not_a_directory(self):
        return NotADirectoryError(_errno.ENOTDIR, "Not a directory", str(self))

    def _scandir(self):
        # Each list_objects_v2 page already carries size/mtime for every
        # object -- reuse it instead of `iterdir()` + a HEAD per child.
        prefix = f"{self.key}/" if self.key else ""
        seen = set()
        empty = True
        paginator = self._client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(
                Bucket=self.bucket, Prefix=prefix, Delimiter="/"
            ):
                # A key that is both an object and a prefix (`x` and `x/y`)
                # lists as the object, agreeing with stat()'s exact-object
                # precedence; the subtree under it is not listed. Keys sort
                # `x` before `x/`, so an earlier page never holds the prefix.
                objects = {
                    obj["Key"][len(prefix) :] for obj in page.get("Contents", [])
                }
                for common in page.get("CommonPrefixes", []):
                    empty = False
                    name = common["Prefix"][len(prefix) :].rstrip("/")
                    if name in objects:
                        continue
                    if name and name not in seen:
                        seen.add(name)
                        yield name, FileStat(is_dir=True)
                for obj in page.get("Contents", []):
                    empty = False
                    name = obj["Key"][len(prefix) :]
                    if name and name not in seen:
                        seen.add(name)
                        lm = obj.get("LastModified")
                        mtime = int(lm.timestamp()) if lm else 0
                        yield name, FileStat(
                            st_size=obj.get("Size", 0) or 0,
                            st_mtime=mtime,
                            is_dir=False,
                        )
        except _S3_ERRORS as error:
            raise _oserror(error, self) from error
        if empty and self.key:
            # Nothing under the prefix: a missing path or a file, which
            # pathlib's iterdir() refuses. Only an empty listing pays this.
            if not self.stat().is_dir():
                raise self._not_a_directory()

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def _open(self, mode="r", buffering=-1):
        if mode in ("r", "r+"):
            try:
                resp = self._client.get_object(Bucket=self.bucket, Key=self.key)
            except _S3_ERRORS as error:
                translated = _oserror(error, self)
                if isinstance(translated, FileNotFoundError) and self.is_dir():
                    translated = IsADirectoryError(
                        _errno.EISDIR, "Is a directory", str(self)
                    )
                raise translated from error
            raw = _S3BodyReader(resp["Body"])
            if mode == "r+":
                # Read-modify-write: what is written is uploaded on close.
                with raw:
                    return _S3WriteStream(self, initial=raw)
            if buffering == 0:
                return raw
            size = _io.DEFAULT_BUFFER_SIZE if buffering in (-1, 1) else buffering
            return _io.BufferedReader(raw, size)
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _S3WriteStream(self, exclusive=(mode == "x"))

    def _put_exclusive(self, body, key=None) -> bool:
        """PutObject that creates `key` (default: this path's) only if it
        does not exist. FileExistsError if it does; False if the store does
        not implement conditional writes, so nothing was written."""
        key = self.key if key is None else key
        try:
            self._client.put_object(
                Bucket=self.bucket, Key=key, Body=body, IfNoneMatch="*"
            )
        except _botoexc.ClientError as error:
            if _is_precondition_failed(error):
                raise FileExistsError(
                    _errno.EEXIST, "File exists", str(self)
                ) from error
            if _is_not_implemented(error):
                return False
            raise _oserror(error, self) from error
        except _TRANSPORT_ERRORS as error:
            raise _oserror(error, self) from error
        return True

    def _mkdir(self, mode):
        if self.exists():
            raise FileExistsError(self)
        marker = f"{self.key}/"
        if not self._put_exclusive(b"", key=marker):
            try:
                self._client.put_object(Bucket=self.bucket, Key=marker, Body=b"")
            except _S3_ERRORS as error:
                raise _oserror(error, self) from error

    def unlink(self, missing_ok=False):
        try:
            st = self.stat()
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if st.is_dir():
            # A prefix: delete_object() of the bare key would delete nothing
            # and report success.
            raise IsADirectoryError(_errno.EISDIR, "Is a directory", str(self))
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self.key)
        except _S3_ERRORS as error:
            raise _oserror(error, self) from error

    def rmdir(self):
        if not self.stat().is_dir():
            raise self._not_a_directory()
        if not self.key:
            raise PermissionError(
                _errno.EACCES, "removing a bucket is not supported", str(self)
            )
        marker = f"{self.key}/"
        try:
            resp = self._client.list_objects_v2(
                Bucket=self.bucket, Prefix=marker, MaxKeys=2
            )
            contents = resp.get("Contents", [])
            if any(obj["Key"] != marker for obj in contents):
                raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(self))
            self._client.delete_object(Bucket=self.bucket, Key=marker)
        except _S3_ERRORS as error:
            raise _oserror(error, self) from error

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
            self._client.head_object(Bucket=self.bucket, Key=self.key)
            keys.append(self.key)
        except _S3_ERRORS as error:
            if not (isinstance(error, _botoexc.ClientError) and _is_not_found(error)):
                translated = _oserror(error, self)
                if not on_error(translated):
                    raise translated from error
                return
        if not keys:
            marker = f"{self.key}/" if self.key else ""
            paginator = self._client.get_paginator("list_objects_v2")
            try:
                for page in paginator.paginate(Bucket=self.bucket, Prefix=marker):
                    keys.extend(obj["Key"] for obj in page.get("Contents", []))
            except _S3_ERRORS as error:
                translated = _oserror(error, self)
                if not on_error(translated):
                    raise translated from error
                return

        if not keys:
            if missing_ok:
                return
            error = FileNotFoundError(self)
            if not on_error(error):
                raise error
            return

        iterator = iter(keys)
        while True:
            batch = list(_itertools.islice(iterator, 1000))
            if not batch:
                break
            try:
                try:
                    response = self._client.delete_objects(
                        Bucket=self.bucket,
                        Delete={"Objects": [{"Key": key} for key in batch]},
                    )
                except _S3_ERRORS as error:
                    raise _oserror(error, self) from error
                errors = response.get("Errors", []) if response else []
                if errors:
                    raise OSError(f"S3 delete_objects failed: {errors!r}")
            except Exception as error:
                if not on_error(error):
                    raise

    def rename(self, target: "S3Path | Uri | str"):
        target = self._rename_target(target)
        dest_key = _object_key(target.path)
        # pathlib returns the new path.
        renamed = self.with_path(target.path)
        if dest_key == self.key:
            return renamed
        client = self._client
        try:
            head = client.head_object(Bucket=self.bucket, Key=self.key)
        except _S3_ERRORS as error:
            if not (isinstance(error, _botoexc.ClientError) and _is_not_found(error)):
                raise _oserror(error, self) from error
            head = None
        if head is None:
            # No object at the key: a prefix directory (stat() raises
            # FileNotFoundError when there is nothing at all). Its keys are
            # not renamed one by one here; move() falls back to copy + rm.
            self._pop_stat_hint()
            self.stat()
            raise NotImplementedError(f"rename() of the prefix directory {self}")
        extra = {}
        storage_class = head.get("StorageClass")
        if storage_class and storage_class != "STANDARD":
            # CopyObject writes STANDARD unless told otherwise.
            extra["StorageClass"] = storage_class
        source = {"Bucket": self.bucket, "Key": self.key}
        managed_copy = getattr(client, "copy", None)
        try:
            if head.get("ContentLength", 0) > _PUT_OBJECT_LIMIT and callable(
                managed_copy
            ):
                # CopyObject refuses sources over 5 GB; the managed copy
                # uses UploadPartCopy.
                managed_copy(source, self.bucket, dest_key, ExtraArgs=extra or None)
            else:
                client.copy_object(
                    Bucket=self.bucket, Key=dest_key, CopySource=source, **extra
                )
            client.delete_object(Bucket=self.bucket, Key=self.key)
        except _S3_ERRORS as error:
            raise _oserror(error, self) from error
        return renamed
