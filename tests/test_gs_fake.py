import datetime

import pytest

from pathlib_next.uri.schemes.gs import BaseGsBackend, GsPath


class _ApiError(Exception):
    """Mirrors `google.api_core.exceptions.GoogleAPICallError`: the HTTP
    status is the `code` attribute (`NotFound.code == 404`), which is what
    `GsPath` classifies by. The real types cannot be raised here -- the SDK
    is not installed in the test venvs."""

    code = None


class _Missing(_ApiError):
    code = 404


class _Forbidden(_ApiError):
    code = 403


class _PreconditionFailed(_ApiError):
    code = 412


class _ServiceUnavailable(_ApiError):
    code = 503


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
        if self.name in self._bucket.reload_errors:
            raise self._bucket.reload_errors[self.name]
        if self.name not in self._bucket.objects:
            raise _Missing(self.name)

    def delete(self):
        if self.name in self._bucket.delete_errors:
            raise OSError(self.name)
        if self.name in self._bucket.delete_raises:
            raise self._bucket.delete_raises[self.name]
        if self.name not in self._bucket.objects:
            raise _Missing(self.name)
        del self._bucket.objects[self.name]
        self._bucket.deleted.append(self.name)

    def download_as_bytes(self):
        if self.name not in self._bucket.objects:
            raise _Missing(self.name)
        return self._bucket.objects[self.name]

    def upload_from_string(self, data, if_generation_match=None):
        if self._bucket.upload_errors:
            raise self._bucket.upload_errors.pop(0)
        if if_generation_match == 0 and self.name in self._bucket.objects:
            raise _PreconditionFailed(self.name)
        self._bucket.objects[self.name] = bytes(data)
        self._bucket.uploads.append(self.name)


class _FakeIterator:
    """`list_blobs()`'s HTTPIterator: blobs when iterated, and with a
    delimiter the common `prefixes` seen while paging."""

    def __init__(self, bucket, prefix, delimiter):
        self._bucket = bucket
        self._prefix = prefix
        self._delimiter = delimiter
        self.prefixes = set()

    def __iter__(self):
        for name in sorted(self._bucket.objects):
            if not name.startswith(self._prefix):
                continue
            rest = name[len(self._prefix) :]
            if self._delimiter and self._delimiter in rest:
                self.prefixes.add(
                    self._prefix + rest.split(self._delimiter, 1)[0] + self._delimiter
                )
                continue
            yield _FakeBlob(self._bucket, name)


class _FakeBucket:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.uploads = []
        self.delete_errors = set()
        self.delete_raises = {}
        self.reload_errors = {}
        self.upload_errors = []
        self.list_calls = []

    def blob(self, name):
        return _FakeBlob(self, name)

    def list_blobs(self, prefix="", delimiter=None, **_kwargs):
        self.list_calls.append(prefix)
        return _FakeIterator(self, prefix, delimiter)

    def copy_blob(self, blob, destination_bucket, new_name):
        if blob.name not in self.objects:
            raise _Missing(blob.name)
        destination_bucket.objects[new_name] = self.objects[blob.name]


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


# --- only a not-found reply means "missing" ---------------------------------


def _bucket_with(**objects):
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update(objects)
    return backend, bucket


def test_transient_reload_error_is_not_file_not_found():
    backend, bucket = _bucket_with(**{"report.csv": b"PRECIOUS"})
    bucket.reload_errors["report.csv"] = _ServiceUnavailable("503 backend error")
    p = _gs("gs://bucket/report.csv", backend)
    with pytest.raises(OSError) as info:
        p.stat()
    assert not isinstance(info.value, FileNotFoundError)
    assert isinstance(info.value.__cause__, _ServiceUnavailable)
    assert bucket.objects == {"report.csv": b"PRECIOUS"}


def test_forbidden_reload_is_permission_error():
    backend, bucket = _bucket_with(**{"a.txt": b"x"})
    bucket.reload_errors["a.txt"] = _Forbidden("403")
    p = _gs("gs://bucket/a.txt", backend)
    with pytest.raises(PermissionError):
        p.stat()
    assert not p.exists()  # pathlib parity: exists() swallows OSError


def test_rm_recursive_transient_reload_error_does_not_delete_prefix_tree():
    backend, bucket = _bucket_with(**{"x": b"object", "x/child": b"child"})
    bucket.reload_errors["x"] = _ServiceUnavailable("503")
    with pytest.raises(OSError):
        _gs("gs://bucket/x", backend).rm(recursive=True)
    assert bucket.objects == {"x": b"object", "x/child": b"child"}


def test_unlink_missing_ok_does_not_hide_a_failed_delete():
    backend, bucket = _bucket_with(**{"held.txt": b"x"})
    bucket.delete_raises["held.txt"] = _Forbidden("403 object under hold")
    p = _gs("gs://bucket/held.txt", backend)
    with pytest.raises(PermissionError):
        p.unlink(missing_ok=True)
    with pytest.raises(PermissionError):
        p.unlink()
    assert "held.txt" in bucket.objects


def test_missing_sdk_is_import_error_not_file_not_found():
    try:
        import google.cloud.storage  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("google-cloud-storage is installed")
    p = GsPath("gs://bucket/a.txt")
    with pytest.raises(ImportError):
        p.read_bytes()
    with pytest.raises(ImportError):
        p.exists()


# --- missing or wrong-type targets raise like pathlib ------------------------


def test_iterdir_missing_and_file_raise():
    backend, _bucket = _bucket_with(**{"file.txt": b"x", "d/": b""})
    with pytest.raises(FileNotFoundError):
        list(_gs("gs://bucket/typo", backend).iterdir())
    with pytest.raises(NotADirectoryError):
        list(_gs("gs://bucket/file.txt", backend).iterdir())
    assert list(_gs("gs://bucket/d", backend).iterdir()) == []


def test_unlink_directory_and_rmdir_wrong_targets():
    import errno

    backend, bucket = _bucket_with(**{"file.txt": b"x", "d/": b"", "d/f": b"y"})
    with pytest.raises(IsADirectoryError):
        _gs("gs://bucket/d", backend).unlink()
    with pytest.raises(FileNotFoundError):
        _gs("gs://bucket/nope", backend).rmdir()
    with pytest.raises(NotADirectoryError):
        _gs("gs://bucket/file.txt", backend).rmdir()
    with pytest.raises(OSError) as info:
        _gs("gs://bucket/d", backend).rmdir()
    assert info.value.errno == errno.ENOTEMPTY
    assert set(bucket.objects) == {"file.txt", "d/", "d/f"}


def test_read_directory_is_a_directory_error():
    backend, _bucket = _bucket_with(**{"d/f": b"y"})
    with pytest.raises(IsADirectoryError):
        _gs("gs://bucket/d", backend).read_bytes()


# --- write streams ----------------------------------------------------------


def test_failed_close_upload_is_not_retried_at_gc():
    import gc

    backend, bucket = _bucket_with()
    bucket.upload_errors.append(_ServiceUnavailable("503"))
    p = _gs("gs://bucket/state.json", backend)
    f = p.open("wb")
    f.write(b'{"version": 1}')
    with pytest.raises(OSError):
        f.close()
    assert f.closed
    p.write_bytes(b'{"version": 2}')
    del f
    gc.collect()
    assert bucket.objects["state.json"] == b'{"version": 2}'


def test_exclusive_create_is_atomic():
    backend, bucket = _bucket_with()
    first = _gs("gs://bucket/job.lock", backend).open("xb")
    second = _gs("gs://bucket/job.lock", backend).open("xb")
    first.write(b"worker1")
    second.write(b"worker2")
    first.close()
    with pytest.raises(FileExistsError):
        second.close()
    assert bucket.objects["job.lock"] == b"worker1"


def test_mkdir_race_is_file_exists():
    backend, bucket = _bucket_with()
    bucket.upload_errors.append(_PreconditionFailed("412"))
    with pytest.raises(FileExistsError):
        _gs("gs://bucket/d", backend).mkdir()


def test_rplus_writes_land_and_read_stream_is_read_only():
    import io

    backend, bucket = _bucket_with(**{"a.txt": b"hello"})
    p = _gs("gs://bucket/a.txt", backend)
    with p.open("rb") as f:
        with pytest.raises(io.UnsupportedOperation):
            f.write(b"x")
    with p.open("r+b") as f:
        assert f.read() == b"hello"
    assert bucket.uploads == []  # unmodified: nothing uploaded
    with p.open("r+b") as f:
        f.write(b"J")
    assert bucket.objects["a.txt"] == b"Jello"


# --- rename/move of a prefix directory ---------------------------------------


def test_rename_prefix_directory_falls_back_in_move():
    backend, bucket = _bucket_with(**{"dir/a": b"a", "dir/sub/b": b"b", "k": b"k"})
    src = _gs("gs://bucket/dir", backend)
    with pytest.raises(NotImplementedError):
        src.rename("dir2")
    with pytest.raises(FileNotFoundError):
        _gs("gs://bucket/nope", backend).rename("nope2")
    src.move(_gs("gs://bucket/dir3", backend))
    # copy(recursive=True) creates the directories as markers.
    assert bucket.objects == {
        "dir3/": b"",
        "dir3/a": b"a",
        "dir3/sub/": b"",
        "dir3/sub/b": b"b",
        "k": b"k",
    }
