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

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None):
        if IfNoneMatch == "*" and Key in self.objects:
            raise _client_error("PreconditionFailed")
        if hasattr(Body, "read"):
            Body = Body.read()
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


# --- destination keys are decoded paths, not URI syntax (0.9.3) ----------
# "?" and "#" are legal S3 key characters; the old
# `Uri(self.parent, target)` truncated the destination key at either one.


@pytest.mark.parametrize("name", ["b?x.txt", "b#x.txt", "b%20x.txt"])
def test_rename_str_destination_key_is_literal(name):
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"content"
    _s3("s3://bucket/a.txt", backend).rename(name)
    assert backend._client.objects.get(name) == b"content"
    assert "a.txt" not in backend._client.objects


# --- a trailing "/" names the directory, not the "dir/" marker object -------
# `s3://bucket/dir/` is how `aws s3 ls` and the console spell a folder. The
# key kept the slash, so the `dir/` marker read as a file and
# `rm(recursive=True)` deleted only the marker while reporting success.


@pytest.mark.parametrize(
    "uri, key",
    [
        ("s3://bucket/dir/", "dir"),
        ("s3://bucket/dir", "dir"),
        ("s3://bucket/a/b/", "a/b"),
        ("s3://bucket/", ""),
        # Exactly one trailing slash goes; interior empty segments are
        # literal key bytes and stay.
        ("s3://bucket/a//b", "a//b"),
        ("s3://bucket/dir//", "dir/"),
    ],
)
def test_key_drops_one_trailing_slash(uri, key):
    assert _s3(uri).key == key


def test_trailing_slash_marker_dir_is_a_directory_fake_client():
    backend = _FakeBackend()
    backend._client.objects.update(
        {"dir/": b"", "dir/a.txt": b"a", "dir/sub/b.txt": b"b", "other": b"k"}
    )
    p = _s3("s3://bucket/dir/", backend)
    assert p.is_dir()
    assert not p.is_file()
    p.rm(recursive=True)
    assert backend._client.objects == {"other": b"k"}


def test_rename_to_trailing_slash_destination_drops_the_slash():
    backend = _FakeBackend()
    backend._client.objects["a.txt"] = b"content"
    _s3("s3://bucket/a.txt", backend).rename(_s3("s3://bucket/b.txt/", backend))
    assert backend._client.objects == {"b.txt": b"content"}


@pytest.fixture
def moto_s3():
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    import os

    from moto import mock_aws

    with mock_aws():
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bkt")
        yield client


def _moto_keys(client):
    listing = client.list_objects_v2(Bucket="bkt").get("Contents", [])
    return sorted(obj["Key"] for obj in listing)


def test_trailing_slash_dir_with_marker_moto(moto_s3):
    for key in ("dir/", "dir/a.txt", "dir/sub/b.txt", "keep.txt"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=b"x" if "." in key else b"")

    p = S3Path("s3://bkt/dir/")
    assert p.key == "dir"
    assert p.exists()
    assert p.is_dir()
    assert not p.is_file()
    assert sorted(child.name for child in p.iterdir()) == ["a.txt", "sub"]

    p.rm(recursive=True)
    assert _moto_keys(moto_s3) == ["keep.txt"]


def test_trailing_slash_dir_without_marker_moto(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="plain/x.txt", Body=b"x")
    moto_s3.put_object(Bucket="bkt", Key="keep.txt", Body=b"k")

    p = S3Path("s3://bkt/plain/")
    assert p.exists()
    assert p.is_dir()
    assert [child.name for child in p.iterdir()] == ["x.txt"]
    p.rm(recursive=True)
    assert _moto_keys(moto_s3) == ["keep.txt"]


def test_trailing_slash_mkdir_then_rmdir_moto(moto_s3):
    p = S3Path("s3://bkt/new/")
    p.mkdir()
    # One "new/" marker, not "new//".
    assert _moto_keys(moto_s3) == ["new/"]
    assert p.is_dir()
    p.rmdir()
    assert _moto_keys(moto_s3) == []


# --- objstore-listing-hides-object-prefix-collision -----------------------------


def test_key_that_is_object_and_prefix_lists_as_the_object(moto_s3):
    from pathlib_next.mempath import MemPath

    moto_s3.put_object(Bucket="bkt", Key="src/logs", Body=b"FILE-CONTENT")
    moto_s3.put_object(Bucket="bkt", Key="src/logs/2026.txt", Body=b"child")
    moto_s3.put_object(Bucket="bkt", Key="src/other/x.txt", Body=b"x")
    src = S3Path("s3://bkt/src")
    listing = dict(src._scandir())
    assert sorted(listing) == ["logs", "other"]
    # Agrees with stat()'s exact-object precedence.
    assert not listing["logs"].is_dir()
    assert listing["logs"].st_size == len(b"FILE-CONTENT")
    assert not S3Path("s3://bkt/src/logs").stat().is_dir()
    dst = MemPath("/dst")
    src.copy(dst, recursive=True)
    assert (dst / "logs").read_bytes() == b"FILE-CONTENT"
    assert (dst / "other" / "x.txt").read_bytes() == b"x"


# --- objstore-open-read-returns-writable-buffer (regression) ------------------


def test_open_rb_is_read_only_and_rplus_writes_land(moto_s3):
    import io

    moto_s3.put_object(Bucket="bkt", Key="f.txt", Body=b"F")
    p = S3Path("s3://bkt/f.txt")
    with p.open("rb") as f:
        assert not f.writable()
        with pytest.raises(io.UnsupportedOperation):
            f.write(b"zz")
    with p.open("r+b") as f:
        f.write(b"N")
    assert p.read_bytes() == b"N"
