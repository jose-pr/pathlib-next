from __future__ import annotations

import io as _io
import itertools as _itertools
import typing as _ty

import botocore.exceptions as _botoexc

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import Source, Uri, UriPath


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


def _is_not_found(error: _botoexc.ClientError) -> bool:
    code = error.response.get("Error", {}).get("Code")
    return code in ("404", "NoSuchKey", "NotFound")


class _S3WriteStream(_io.BytesIO):
    def __init__(self, path: "S3Path"):
        super().__init__()
        self._path = path

    def close(self):
        if not self.closed:
            self._path._client.put_object(
                Bucket=self._path.bucket, Key=self._path.key, Body=self.getvalue()
            )
        super().close()


class S3Path(UriPath):
    """`s3:` scheme (`s3://bucket/key/path`): read/write/list via `boto3`.
    Requires the `s3` extra. S3 has no real directories -- `is_dir()` is
    prefix emulation (any object key under `"<path>/"`), and `mkdir()`
    creates a zero-byte `"<path>/"` marker object (the same convention the
    AWS console itself uses for an empty "folder"); see
    `docs/divergences.md`. `rename()` uses server-side `copy_object` +
    `delete_object` (same-bucket only) instead of the generic
    download+upload+delete `move()` fallback."""

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
        return self.path.lstrip("/")

    @property
    def _client(self):
        return self.backend.client()

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        key = self.key
        if key == "":
            try:
                self._client.head_bucket(Bucket=self.bucket)
            except _botoexc.ClientError as error:
                raise FileNotFoundError(self) from error
            return FileStat(is_dir=True)
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key)
            return FileStat(
                st_size=head["ContentLength"],
                st_mtime=int(head["LastModified"].timestamp()),
                is_dir=False,
            )
        except _botoexc.ClientError as error:
            if not _is_not_found(error):
                raise
        # Not an object at this exact key -- emulate a directory: any
        # object under the "<key>/" prefix means this is a "directory".
        resp = self._client.list_objects_v2(
            Bucket=self.bucket, Prefix=f"{key}/", MaxKeys=1
        )
        if resp.get("KeyCount", 0) > 0:
            return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _scandir(self):
        # Each list_objects_v2 page already carries size/mtime for every
        # object -- reuse it instead of `iterdir()` + a HEAD per child.
        prefix = f"{self.key}/" if self.key else ""
        seen = set()
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=prefix, Delimiter="/"
        ):
            for common in page.get("CommonPrefixes", []):
                name = common["Prefix"][len(prefix) :].rstrip("/")
                if name and name not in seen:
                    seen.add(name)
                    yield name, FileStat(is_dir=True)
            for obj in page.get("Contents", []):
                name = obj["Key"][len(prefix) :]
                if name and name not in seen:
                    seen.add(name)
                    lm = obj.get("LastModified")
                    mtime = int(lm.timestamp()) if lm else 0
                    yield name, FileStat(
                        st_size=obj.get("Size", 0) or 0, st_mtime=mtime, is_dir=False
                    )

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def _open(self, mode="r", buffering=-1):
        if "r" in mode:
            try:
                resp = self._client.get_object(Bucket=self.bucket, Key=self.key)
            except _botoexc.ClientError as error:
                raise FileNotFoundError(self) from error
            return _io.BytesIO(resp["Body"].read())
        if mode not in ("w", "x"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _S3WriteStream(self)

    def _mkdir(self, mode):
        if self.exists():
            raise FileExistsError(self)
        self._client.put_object(Bucket=self.bucket, Key=f"{self.key}/", Body=b"")

    def unlink(self, missing_ok=False):
        if not missing_ok and not self.exists():
            raise FileNotFoundError(self)
        # delete_object() is idempotent (no error for a missing key) --
        # the exists() check above is what actually enforces missing_ok.
        self._client.delete_object(Bucket=self.bucket, Key=self.key)

    def rmdir(self):
        marker = f"{self.key}/"
        resp = self._client.list_objects_v2(Bucket=self.bucket, Prefix=marker, MaxKeys=2)
        contents = resp.get("Contents", [])
        if any(obj["Key"] != marker for obj in contents):
            raise OSError(f"Directory not empty: {self}")
        self._client.delete_object(Bucket=self.bucket, Key=marker)

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
        except _botoexc.ClientError as error:
            if not _is_not_found(error):
                if not on_error(error):
                    raise
                return
        if not keys:
            marker = f"{self.key}/" if self.key else ""
            paginator = self._client.get_paginator("list_objects_v2")
            try:
                for page in paginator.paginate(Bucket=self.bucket, Prefix=marker):
                    keys.extend(obj["Key"] for obj in page.get("Contents", []))
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

        iterator = iter(keys)
        while True:
            batch = list(_itertools.islice(iterator, 1000))
            if not batch:
                break
            try:
                response = self._client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": key} for key in batch]},
                )
                errors = response.get("Errors", []) if response else []
                if errors:
                    raise OSError(f"S3 delete_objects failed: {errors!r}")
            except Exception as error:
                if not on_error(error):
                    raise

    def rename(self, target: "S3Path | Uri | str"):
        if not isinstance(target, Uri):
            target = Uri(self.parent, target)
        dest_key = target.path.lstrip("/")
        self._client.copy_object(
            Bucket=self.bucket,
            Key=dest_key,
            CopySource={"Bucket": self.bucket, "Key": self.key},
        )
        self._client.delete_object(Bucket=self.bucket, Key=self.key)
