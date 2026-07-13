"""Unit-only S3 tests: a fake boto3-shaped client via BaseS3Backend, no
real AWS account. Covers prefix-emulated directories, list_objects_v2
paging shape, and copy_object+delete_object rename.
"""
import datetime
import io

import pytest

botocore = pytest.importorskip("botocore")
import botocore.exceptions as _botoexc

from pathlib_next.uri.schemes.s3 import BaseS3Backend, S3Path


def _client_error(code):
    return _botoexc.ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


class _Paginator:
    def __init__(self, client):
        self._client = client

    def paginate(self, **kwargs):
        yield self._client.list_objects_v2(**kwargs)


class _FakeS3Client:
    def __init__(self):
        self.objects = {}  # key -> bytes
        self.delete_errors = []

    def head_bucket(self, Bucket):
        return {}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _client_error("404")
        data = self.objects[Key]
        return {
            "ContentLength": len(data),
            "LastModified": datetime.datetime(2026, 1, 1, 12, 0, 0),
        }

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, MaxKeys=None):
        contents = []
        common = set()
        for key in sorted(self.objects):
            if not key.startswith(Prefix):
                continue
            rest = key[len(Prefix) :]
            if Delimiter and Delimiter in rest:
                common.add(Prefix + rest.split(Delimiter, 1)[0] + Delimiter)
            else:
                contents.append({"Key": key})
        if MaxKeys:
            contents = contents[:MaxKeys]
        result = {"KeyCount": len(contents) + len(common)}
        if contents:
            result["Contents"] = contents
        if common:
            result["CommonPrefixes"] = [{"Prefix": p} for p in sorted(common)]
        return result

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(self)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _client_error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body if isinstance(Body, bytes) else bytes(Body)

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {"Deleted": Delete["Objects"], "Errors": self.delete_errors}

    def copy_object(self, Bucket, Key, CopySource):
        self.objects[Key] = self.objects[CopySource["Key"]]


class _FakeBackend(BaseS3Backend):
    def __init__(self):
        self._client = _FakeS3Client()

    def client(self):
        return self._client


def _s3(uri, backend=None):
    return S3Path(uri, backend=backend or _FakeBackend())


def test_scheme_dispatch():
    assert isinstance(_s3("s3://bucket/a"), S3Path)


def test_bucket_and_key():
    p = _s3("s3://bucket/docs/readme.txt")
    assert p.bucket == "bucket"
    assert p.key == "docs/readme.txt"


def test_stat_file():
    backend = _FakeBackend()
    backend._client.objects["docs/readme.txt"] = b"hello world"
    st = _s3("s3://bucket/docs/readme.txt", backend).stat()
    assert st.st_size == 11
    assert not st.is_dir()


def test_stat_dir_via_prefix():
    backend = _FakeBackend()
    backend._client.objects["docs/readme.txt"] = b"hello"
    assert _s3("s3://bucket/docs", backend).stat().is_dir()


def test_stat_root_is_dir():
    assert _s3("s3://bucket/", _FakeBackend()).stat().is_dir()


def test_stat_missing_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        _s3("s3://bucket/missing.txt", _FakeBackend()).stat()


def test_listdir_root_and_nested():
    backend = _FakeBackend()
    backend._client.objects["docs/readme.txt"] = b"x"
    backend._client.objects["docs/sub/file.txt"] = b"y"
    backend._client.objects["top.txt"] = b"z"
    root = _s3("s3://bucket/", backend)
    assert sorted(root._listdir()) == ["docs", "top.txt"]
    docs = _s3("s3://bucket/docs", backend)
    assert sorted(docs._listdir()) == ["readme.txt", "sub"]


def test_read_bytes():
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"hello"
    assert _s3("s3://bucket/a.txt", backend).read_bytes() == b"hello"


def test_read_missing_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        _s3("s3://bucket/missing.txt", _FakeBackend()).read_bytes()


def test_write_bytes_uploads_via_put_object_on_close():
    backend = _FakeBackend()
    _s3("s3://bucket/new.txt", backend).write_bytes(b"new content")
    assert backend._client.objects["new.txt"] == b"new content"


def test_mkdir_creates_marker_object():
    backend = _FakeBackend()
    _s3("s3://bucket/newdir", backend).mkdir()
    assert "newdir/" in backend._client.objects


def test_mkdir_existing_raises_file_exists():
    backend = _FakeBackend()
    backend._client.objects["newdir/"] = b""
    with pytest.raises(FileExistsError):
        _s3("s3://bucket/newdir", backend).mkdir()


def test_unlink_deletes_object():
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"x"
    _s3("s3://bucket/a.txt", backend).unlink()
    assert "a.txt" not in backend._client.objects


def test_unlink_missing_without_missing_ok_raises():
    with pytest.raises(FileNotFoundError):
        _s3("s3://bucket/missing.txt", _FakeBackend()).unlink()


def test_unlink_missing_ok():
    _s3("s3://bucket/missing.txt", _FakeBackend()).unlink(missing_ok=True)


def test_rmdir_empty_deletes_marker():
    backend = _FakeBackend()
    backend._client.objects["dir/"] = b""
    _s3("s3://bucket/dir", backend).rmdir()
    assert "dir/" not in backend._client.objects


def test_rmdir_non_empty_raises_oserror():
    backend = _FakeBackend()
    backend._client.objects["dir/"] = b""
    backend._client.objects["dir/file.txt"] = b"x"
    with pytest.raises(OSError):
        _s3("s3://bucket/dir", backend).rmdir()


def test_rm_recursive_uses_batch_delete_for_prefix():
    backend = _FakeBackend()
    backend._client.objects.update(
        {
            "dir/": b"",
            "dir/a.txt": b"a",
            "dir/sub/b.txt": b"b",
            "other.txt": b"keep",
        }
    )
    _s3("s3://bucket/dir", backend).rm(recursive=True)
    assert backend._client.objects == {"other.txt": b"keep"}


def test_rm_recursive_deletes_exact_object_key():
    backend = _FakeBackend()
    backend._client.objects.update(
        {
            "file.txt": b"x",
            "file.txt/nested.txt": b"keep",
        }
    )
    _s3("s3://bucket/file.txt", backend).rm(recursive=True)
    assert backend._client.objects == {"file.txt/nested.txt": b"keep"}


def test_rm_recursive_missing_ok():
    _s3("s3://bucket/missing", _FakeBackend()).rm(recursive=True, missing_ok=True)


def test_rm_recursive_missing_without_missing_ok_raises():
    with pytest.raises(FileNotFoundError):
        _s3("s3://bucket/missing", _FakeBackend()).rm(recursive=True)


def test_rm_recursive_ignore_error_swallows_missing():
    calls = []
    _s3("s3://bucket/missing", _FakeBackend()).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append((type(err), path.key)) or True,
    )
    assert calls == [(FileNotFoundError, "missing")]


def test_rm_recursive_root_guard():
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"x"
    with pytest.raises(PermissionError):
        _s3("s3://bucket/", backend).rm(recursive=True)
    assert backend._client.objects == {"a.txt": b"x"}


def test_rm_recursive_delete_objects_errors_raise():
    backend = _FakeBackend()
    backend._client.objects["dir/a.txt"] = b"x"
    backend._client.delete_errors = [{"Key": "dir/a.txt", "Code": "AccessDenied"}]
    with pytest.raises(OSError, match="delete_objects failed"):
        _s3("s3://bucket/dir", backend).rm(recursive=True)


def test_rm_recursive_delete_objects_errors_ignore_error():
    backend = _FakeBackend()
    backend._client.objects["dir/a.txt"] = b"x"
    backend._client.delete_errors = [{"Key": "dir/a.txt", "Code": "AccessDenied"}]
    calls = []
    _s3("s3://bucket/dir", backend).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append((type(err), path.key)) or True,
    )
    assert calls == [(OSError, "dir")]


def test_rm_non_recursive_keeps_rmdir_contract():
    backend = _FakeBackend()
    backend._client.objects["dir/file.txt"] = b"x"
    with pytest.raises(OSError):
        _s3("s3://bucket/dir", backend).rm()


def test_rename_uses_copy_then_delete():
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"content"
    _s3("s3://bucket/a.txt", backend).rename("b.txt")
    assert backend._client.objects.get("b.txt") == b"content"
    assert "a.txt" not in backend._client.objects


def test_chmod_not_implemented():
    with pytest.raises(NotImplementedError):
        _s3("s3://bucket/a.txt").chmod(0o644)
