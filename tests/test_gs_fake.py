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


# --- objstore-listing-hides-object-prefix-collision -----------------------------


def test_key_that_is_object_and_prefix_lists_as_the_object():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update(
        {"src/logs": b"FILE-CONTENT", "src/logs/2026.txt": b"child", "src/d/x": b"x"}
    )
    listing = dict(_gs("gs://bucket/src", backend)._scandir())
    assert sorted(listing) == ["d", "logs"]
    assert not listing["logs"].is_dir()
    assert listing["logs"].st_size == len(b"FILE-CONTENT")
    assert listing["d"].is_dir()
    assert not _gs("gs://bucket/src/logs", backend).stat().is_dir()


# --- objstore-gs-az-root-always-exists ------------------------------------------


class _MissingBucket(_FakeBucket):
    def list_blobs(self, prefix="", delimiter=None, **_kwargs):
        raise _Missing("bucket not found")


def test_missing_bucket_root_does_not_exist():
    backend = _FakeBackend()
    backend.client_obj.bucket_obj = _MissingBucket()
    root = _gs("gs://typo-bucket/", backend)
    with pytest.raises(FileNotFoundError):
        root.stat()
    assert not root.exists()
    assert not root.is_dir()


def test_existing_bucket_root_is_a_directory_even_when_empty():
    backend = _FakeBackend()
    root = _gs("gs://bucket", backend)
    assert root.stat().is_dir()
    assert backend.client_obj.bucket_obj.list_calls == [""]


def test_bucket_root_permission_error_is_not_a_directory():
    class _Forbidding(_FakeBucket):
        def list_blobs(self, prefix="", delimiter=None, **_kwargs):
            raise _Forbidden("no list permission")

    backend = _FakeBackend()
    backend.client_obj.bucket_obj = _Forbidding()
    with pytest.raises(PermissionError):
        _gs("gs://bucket/", backend).stat()


# --- a listed name is one component inside the directory that listed it ------


def test_listing_skips_names_that_are_not_one_component():
    backend = _FakeBackend()
    backend.client_obj.bucket_obj.objects.update(
        {
            "dir/ok.txt": b"x",
            "dir/..": b"x",
            "dir/.": b"x",
            "dir/../up/f": b"x",
            "dir/./down/f": b"x",
            "dir/sub/f": b"x",
        }
    )
    listing = dict(_gs("gs://bucket/dir", backend)._scandir())
    assert sorted(listing) == ["ok.txt", "sub"]
    assert listing["sub"].is_dir()


class _RawListing(list):
    """What `list_blobs(delimiter="/")` returns: the blobs, and `prefixes`."""

    def __init__(self, blobs, prefixes):
        super().__init__(blobs)
        self.prefixes = prefixes


def test_listing_skips_names_a_server_reports_with_a_separator():
    class _Raw(_FakeBucket):
        def list_blobs(self, prefix="", delimiter=None, **_kwargs):
            blobs = [_FakeBlob(self, name) for name in ("dir/ok.txt", "dir/a/b")]
            self.objects.update({blob.name: b"x" for blob in blobs})
            return _RawListing(blobs, ["dir/sub/", "dir/p/q/", "dir//"])

    backend = _FakeBackend()
    backend.client_obj.bucket_obj = _Raw()
    listing = dict(_gs("gs://bucket/dir", backend)._scandir())
    assert sorted(listing) == ["ok.txt", "sub"]


# --- a prefix is moved, copied and removed as the same set of objects ---------

_ODD_PREFIXES = {
    "an-empty-segment": {"d/x.txt": b"x", "d//y.txt": b"y", "d/sub//z.txt": b"z"},
    "an-object-that-is-also-a-prefix": {
        "d/x.txt": b"x",
        "d/logs": b"log",
        "d/logs/2026.txt": b"c",
        "d/logs/deep/z.txt": b"z",
    },
    "a-slash-name-that-holds-data": {"d/x.txt": b"x", "d/blob/": b"data"},
    "a-dot-segment": {"d/x.txt": b"x", "d/sub/../z.txt": b"z"},
}


@pytest.fixture(params=list(_ODD_PREFIXES))
def odd_prefix(request):
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update(_ODD_PREFIXES[request.param])
    bucket.objects["unrelated.txt"] = b"keep"
    return backend, bucket, dict(bucket.objects)


def test_rm_recursive_of_a_prefix_with_hidden_objects_removes_none(odd_prefix):
    backend, bucket, before = odd_prefix
    with pytest.raises(OSError, match="nothing was changed"):
        _gs("gs://bucket/d", backend).rm(recursive=True)
    assert bucket.objects == before
    assert bucket.deleted == []


def test_copy_recursive_of_a_prefix_with_hidden_objects_is_refused(odd_prefix):
    from pathlib_next.mempath import MemPath

    backend, bucket, before = odd_prefix
    target = MemPath("/copied")
    with pytest.raises(OSError, match="nothing was changed"):
        _gs("gs://bucket/d", backend).copy(target, recursive=True)
    assert not target.exists()
    assert bucket.objects == before


def test_move_of_a_prefix_with_hidden_objects_keeps_every_object(odd_prefix):
    backend, bucket, before = odd_prefix
    with pytest.raises(OSError, match="nothing was changed"):
        _gs("gs://bucket/d", backend).move(_gs("gs://bucket/moved", backend))
    assert bucket.objects == before


def test_the_refusal_names_the_objects_a_listing_does_not_show():
    backend = _FakeBackend()
    backend.client_obj.bucket_obj.objects.update(
        {"d/x.txt": b"x", "d//y.txt": b"y", "d/logs": b"l", "d/logs/2026.txt": b"c"}
    )
    with pytest.raises(OSError) as raised:
        _gs("gs://bucket/d", backend).rm(recursive=True)
    assert "'d//y.txt'" in str(raised.value)
    assert "'d/logs/2026.txt'" in str(raised.value)
    assert "'d/x.txt'" not in str(raised.value)


def test_rm_recursive_with_ignore_error_removes_only_the_objects_a_listing_shows():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update(
        {"d/": b"", "d/x.txt": b"x", "d//y.txt": b"y", "d/logs": b"l", "d/logs/z": b"z"}
    )
    seen = []
    _gs("gs://bucket/d", backend).rm(
        recursive=True, ignore_error=lambda err, path: seen.append(err) or True
    )
    assert len(seen) == 1 and "'d//y.txt'" in str(seen[0])
    assert sorted(bucket.objects) == ["d//y.txt", "d/logs/z"]


def test_move_of_a_prefix_a_listing_shows_moves_every_object():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update(
        {"d/": b"", "d/x.txt": b"x", "d/sub/a.txt": b"a", "d/empty/": b""}
    )
    _gs("gs://bucket/d", backend).move(_gs("gs://bucket/moved", backend))
    assert sorted(bucket.objects) == [
        "moved/",
        "moved/empty/",
        "moved/sub/",
        "moved/sub/a.txt",
        "moved/x.txt",
    ]


# --- a write or rename never turns a prefix directory into an object ----------


def test_write_onto_a_prefix_directory_is_refused():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update({"d/": b"", "d/x.txt": b"x"})
    with pytest.raises(IsADirectoryError):
        _gs("gs://bucket/d", backend).write_bytes(b"clobber")
    assert bucket.objects == {"d/": b"", "d/x.txt": b"x"}
    assert bucket.uploads == []


def test_rename_onto_a_prefix_directory_is_refused():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update({"b.txt": b"b", "dst/keep.txt": b"k"})
    with pytest.raises(IsADirectoryError):
        _gs("gs://bucket/b.txt", backend).rename("dst")
    assert bucket.objects == {"b.txt": b"b", "dst/keep.txt": b"k"}


def test_write_and_rename_onto_an_object_still_replace_it():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects.update({"a.txt": b"a", "b.txt": b"b"})
    _gs("gs://bucket/a.txt", backend).write_bytes(b"new")
    assert bucket.objects["a.txt"] == b"new"
    _gs("gs://bucket/b.txt", backend).rename("a.txt")
    assert bucket.objects == {"a.txt": b"b"}


def test_write_where_the_prefix_cannot_be_listed_still_writes():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj

    def denied(prefix="", delimiter=None, **_kwargs):
        raise _Forbidden("403 list denied")

    bucket.list_blobs = denied
    _gs("gs://bucket/w.txt", backend).write_bytes(b"written")
    assert bucket.objects == {"w.txt": b"written"}


# --- mkdir treats only "not found" as absent ------------------------------------


def test_mkdir_does_not_create_a_marker_when_the_probe_fails():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects["existing.txt"] = b"keep"
    bucket.reload_errors["existing.txt"] = _ServiceUnavailable("503")
    with pytest.raises(OSError) as raised:
        _gs("gs://bucket/existing.txt", backend).mkdir()
    assert not isinstance(raised.value, FileExistsError)
    assert bucket.objects == {"existing.txt": b"keep"}
    assert bucket.uploads == []


def test_mkdir_on_an_object_is_file_exists_and_on_a_missing_path_writes_the_marker():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects["existing.txt"] = b"keep"
    with pytest.raises(FileExistsError):
        _gs("gs://bucket/existing.txt", backend).mkdir()
    _gs("gs://bucket/newdir", backend).mkdir()
    assert sorted(bucket.objects) == ["existing.txt", "newdir/"]


# --- removing a bucket is not supported ----------------------------------------


def test_rmdir_of_the_bucket_root_is_refused():
    backend = _FakeBackend()
    bucket = backend.client_obj.bucket_obj
    bucket.objects["a.txt"] = b"x"
    root = _gs("gs://bucket/", backend)
    with pytest.raises(PermissionError):
        root.rmdir()
    with pytest.raises(PermissionError):
        root.rm()
    assert bucket.objects == {"a.txt": b"x"}
    assert bucket.deleted == []
