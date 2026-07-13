import datetime

import pytest

from pathlib_next.uri.schemes.gs import BaseGsBackend, GsPath


class _Missing(Exception):
    pass


class _FakeBlob:
    def __init__(self, bucket, name):
        self._bucket = bucket
        self.name = name

    @property
    def size(self):
        return len(self._bucket.objects[self.name])

    @property
    def updated(self):
        return datetime.datetime(2026, 1, 1, 12, 0, 0)

    def reload(self):
        if self.name not in self._bucket.objects:
            raise _Missing(self.name)

    def delete(self):
        if self.name in self._bucket.delete_errors:
            raise OSError(self.name)
        if self.name not in self._bucket.objects:
            raise _Missing(self.name)
        del self._bucket.objects[self.name]
        self._bucket.deleted.append(self.name)

    def upload_from_string(self, data):
        self._bucket.objects[self.name] = data


class _FakeBucket:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.delete_errors = set()
        self.list_calls = []

    def blob(self, name):
        return _FakeBlob(self, name)

    def list_blobs(self, prefix="", **_kwargs):
        self.list_calls.append(prefix)
        for name in sorted(self.objects):
            if name.startswith(prefix):
                yield _FakeBlob(self, name)


class _FakeClient:
    def __init__(self):
        self.bucket_obj = _FakeBucket()

    def bucket(self, _name):
        return self.bucket_obj


class _FakeBackend(BaseGsBackend):
    def __init__(self):
        self.client_obj = _FakeClient()

    def client(self):
        return self.client_obj


def _gs(uri, backend=None):
    return GsPath(uri, backend=backend or _FakeBackend())


def test_rm_recursive_deletes_prefix_tree():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update(
        {
            "dir/": b"",
            "dir/a.txt": b"a",
            "dir/sub/b.txt": b"b",
            "other.txt": b"keep",
        }
    )
    _gs("gs://bucket/dir", backend).rm(recursive=True)
    assert bucket.objects == {"other.txt": b"keep"}
    assert bucket.list_calls == ["dir/"]


def test_rm_recursive_deletes_exact_object_before_prefix():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update(
        {
            "file.txt": b"x",
            "file.txt/nested.txt": b"keep",
        }
    )
    _gs("gs://bucket/file.txt", backend).rm(recursive=True)
    assert bucket.objects == {"file.txt/nested.txt": b"keep"}
    assert bucket.list_calls == []


def test_rm_recursive_missing_ok():
    _gs("gs://bucket/missing", _FakeBackend()).rm(recursive=True, missing_ok=True)


def test_rm_recursive_missing_without_missing_ok_raises():
    with pytest.raises(FileNotFoundError):
        _gs("gs://bucket/missing", _FakeBackend()).rm(recursive=True)


def test_rm_recursive_ignore_error_swallows_missing():
    calls = []
    _gs("gs://bucket/missing", _FakeBackend()).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append((type(err), path.key)) or True,
    )
    assert calls == [(FileNotFoundError, "missing")]


def test_rm_recursive_root_guard():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects["a.txt"] = b"x"
    with pytest.raises(PermissionError):
        _gs("gs://bucket/", backend).rm(recursive=True)
    assert bucket.objects == {"a.txt": b"x"}


def test_rm_recursive_delete_error_reroutes_to_ignore_error():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects["dir/a.txt"] = b"x"
    bucket.delete_errors.add("dir/a.txt")
    calls = []
    _gs("gs://bucket/dir", backend).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append((type(err), path.key)) or True,
    )
    assert calls == [(OSError, "dir")]
