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


# --- a trailing "/" names the directory, not the "dir/" marker object -------


@pytest.mark.parametrize(
    "uri, key",
    [
        ("gs://bucket/dir/", "dir"),
        ("gs://bucket/dir", "dir"),
        ("gs://bucket/", ""),
        ("gs://bucket/a//b", "a//b"),
        ("gs://bucket/dir//", "dir/"),
    ],
)
def test_key_drops_one_trailing_slash(uri, key):
    assert _gs(uri).key == key


def test_trailing_slash_marker_dir_is_a_directory_and_rm_deletes_tree():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update(
        {"dir/": b"", "dir/a.txt": b"a", "dir/sub/b.txt": b"b", "other.txt": b"k"}
    )
    p = _gs("gs://bucket/dir/", backend)
    assert p.is_dir()
    assert not p.is_file()
    p.rm(recursive=True)
    assert bucket.objects == {"other.txt": b"k"}


# --- GsBackend passes client options through, never touching os.environ ----


@pytest.fixture
def fake_storage_module(monkeypatch):
    """A stand-in `google.cloud.storage` that records the Client kwargs, so
    the backend is checked without the SDK installed."""
    import importlib
    import sys
    import types

    received = []

    class Client:
        def __init__(self, **kwargs):
            received.append(kwargs)

    storage = types.ModuleType("google.cloud.storage")
    storage.Client = Client
    for name in ("google", "google.cloud"):
        try:
            importlib.import_module(name)
        except ImportError:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)
    monkeypatch.setattr(sys.modules["google.cloud"], "storage", storage, raising=False)
    return received


def test_gs_backend_does_not_mutate_environment(fake_storage_module, monkeypatch):
    import os

    from pathlib_next.uri.schemes.gs import GsBackend

    monkeypatch.delenv("STORAGE_EMULATOR_HOST", raising=False)
    options = {
        "api_endpoint": "https://storage-acme.p.googleapis.com",
        "quota_project_id": "billing-proj",
    }
    GsBackend(client_options=options, project="prod").client()
    assert "STORAGE_EMULATOR_HOST" not in os.environ
    # Every option reaches the client, api_endpoint included.
    assert fake_storage_module == [
        {
            "client_options": {
                "api_endpoint": "https://storage-acme.p.googleapis.com",
                "quota_project_id": "billing-proj",
            },
            "project": "prod",
        }
    ]

    # A later, unrelated backend is not redirected.
    GsBackend(project="unrelated").client()
    assert "STORAGE_EMULATOR_HOST" not in os.environ
    assert fake_storage_module[-1] == {"project": "unrelated"}


def test_gs_backend_caches_its_client(fake_storage_module):
    from pathlib_next.uri.schemes.gs import GsBackend

    backend = GsBackend(project="p")
    assert backend.client() is backend.client()
    assert len(fake_storage_module) == 1
