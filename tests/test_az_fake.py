import datetime

import pytest

from pathlib_next.uri.schemes.az import AzPath, BaseAzBackend


class _HttpResponseError(Exception):
    """Mirrors `azure.core.exceptions.HttpResponseError`: the HTTP status is
    the `status_code` attribute, which is what `AzPath` classifies by. The
    real types cannot be raised here -- the SDK is not installed in the test
    venvs."""

    status_code = None


class _Missing(_HttpResponseError):  # ResourceNotFoundError
    status_code = 404


class _Exists(_HttpResponseError):  # ResourceExistsError
    status_code = 409


class _LeaseIdMissing(_HttpResponseError):
    status_code = 412


class _ServiceUnavailable(_HttpResponseError):
    status_code = 503


class _Forbidden(_HttpResponseError):
    status_code = 403


class _FakeBlobItem:
    def __init__(self, name, data):
        self.name = name
        self.size = len(data)
        self.last_modified = datetime.datetime(2026, 1, 1, 12, 0, 0)


class _FakeDownloader:
    def __init__(self, data):
        self._data = data

    def readall(self):
        return self._data


class _FakeBlobClient:
    def __init__(self, container, name):
        self._container = container
        self.name = name
        self.url = f"https://example.invalid/{name}"

    def get_blob_properties(self):
        if self.name in self._container.properties_errors:
            raise self._container.properties_errors[self.name]
        if self.name not in self._container.objects:
            raise _Missing(self.name)
        return {
            "size": len(self._container.objects[self.name]),
            "last_modified": datetime.datetime(2026, 1, 1, 12, 0, 0),
        }

    def delete_blob(self):
        if self.name in self._container.delete_errors:
            raise OSError(self.name)
        if self.name in self._container.delete_raises:
            raise self._container.delete_raises[self.name]
        if self.name not in self._container.objects:
            raise _Missing(self.name)
        del self._container.objects[self.name]
        self._container.deleted.append(self.name)

    def download_blob(self):
        if self.name not in self._container.objects:
            raise _Missing(self.name)
        return _FakeDownloader(self._container.objects[self.name])

    def upload_blob(self, data, overwrite=False):
        if self._container.upload_errors:
            raise self._container.upload_errors.pop(0)
        # The real SDK sends If-None-Match: * unless overwrite=True.
        if not overwrite and self.name in self._container.objects:
            raise _Exists(self.name)
        self._container.objects[self.name] = bytes(data)
        self._container.uploads.append(self.name)

    def start_copy_from_url(self, url):
        source = url.rsplit("/", 1)[-1]
        self._container.objects[self.name] = self._container.objects[source]
        return {"copy_status": "success"}


class _FakeContainer:
    def __init__(self, *, bulk=True):
        self.objects = {}
        self.deleted = []
        self.uploads = []
        self.delete_errors = set()
        self.delete_raises = {}
        self.properties_errors = {}
        self.upload_errors = []
        self.batch_rejected = False
        self.list_calls = []
        self.bulk_delete_calls = []
        if not bulk:
            self.delete_blobs = None

    def get_blob_client(self, name):
        return _FakeBlobClient(self, name)

    def list_blobs(self, name_starts_with="", **_kwargs):
        self.list_calls.append(name_starts_with)
        for name in sorted(self.objects):
            if name.startswith(name_starts_with):
                yield _FakeBlobItem(name, self.objects[name])

    def walk_blobs(self, name_starts_with="", delimiter="/"):
        from azure.storage.blob import BlobPrefix

        # Like the SDK, every prefix of a page comes before its blobs, in
        # whatever order the service listed them.
        prefixes = []
        blobs = []
        for name in sorted(self.objects):
            if not name.startswith(name_starts_with):
                continue
            rest = name[len(name_starts_with) :]
            if delimiter in rest:
                prefix = name_starts_with + rest.split(delimiter, 1)[0] + delimiter
                if prefix not in prefixes:
                    prefixes.append(prefix)
                continue
            blobs.append(_FakeBlobItem(name, self.objects[name]))
        for prefix in prefixes:
            yield BlobPrefix(prefix=prefix)
        yield from blobs

    def delete_blobs(self, *names):
        # Like the real SDK: a rejected batch deletes nothing; otherwise
        # every sub-request runs, then a partial failure raises
        # (PartialBatchErrorException).
        self.bulk_delete_calls.append(names)
        if self.batch_rejected:
            raise _HttpResponseError("batch endpoint rejected")
        failed = []
        for name in names:
            if name in self.delete_errors:
                failed.append(name)
                continue
            if name in self.objects:
                del self.objects[name]
                self.deleted.append(name)
        if failed:
            raise OSError(f"partial batch failure: {failed}")


class _FakeClient:
    def __init__(self, *, bulk=True):
        self.container = _FakeContainer(bulk=bulk)

    def get_container_client(self, _name):
        return self.container


class _FakeBackend(BaseAzBackend):
    def __init__(self, *, bulk=True):
        self.client_obj = _FakeClient(bulk=bulk)

    def client(self):
        return self.client_obj


@pytest.fixture
def fake_blob_module(monkeypatch):
    """A stand-in `azure.storage.blob` (BlobPrefix for `walk_blobs()`, and a
    BlobServiceClient that records how it was built) when the SDK is not
    installed; the real module is used, unrecorded, when it is."""
    import importlib
    import sys
    import types

    from pathlib_next.uri.schemes import az

    # Default backends are cached per account; keep this test's out of it.
    monkeypatch.setattr(az, "_DEFAULT_BACKENDS", {})
    try:
        importlib.import_module("azure.storage.blob")
    except ImportError:
        pass
    else:
        pytest.skip("azure-storage-blob is installed")
    built = []

    class BlobPrefix:
        def __init__(self, *args, prefix=None, **kwargs):
            self.name = prefix

    class BlobServiceClient:
        def __init__(self, account_url, credential=None, **kwargs):
            built.append(("init", account_url, credential, kwargs))

        @classmethod
        def from_connection_string(cls, conn_str, credential=None, **kwargs):
            built.append(("conn", conn_str, credential, kwargs))
            return object.__new__(cls)

    blob = types.ModuleType("azure.storage.blob")
    blob.BlobPrefix = BlobPrefix
    blob.BlobServiceClient = BlobServiceClient
    for name in ("azure", "azure.storage"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "azure.storage.blob", blob)
    return built


@pytest.fixture
def walk_sdk(monkeypatch):
    """`azure.storage.blob.BlobPrefix`, which a `walk_blobs()` listing is made
    of: the SDK's own when it is installed, else a stand-in that is named from
    its `prefix=` argument as the SDK's is."""
    import importlib
    import sys
    import types

    try:
        importlib.import_module("azure.storage.blob")
    except ImportError:
        pass
    else:
        return

    class BlobPrefix:
        def __init__(self, *args, prefix=None, **kwargs):
            self.name = prefix

    blob = types.ModuleType("azure.storage.blob")
    blob.BlobPrefix = BlobPrefix
    for name in ("azure", "azure.storage"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "azure.storage.blob", blob)


def _az(uri, backend=None):
    return AzPath(uri, backend=backend or _FakeBackend())


def test_rm_recursive_deletes_prefix_tree_with_bulk_delete():
    backend = _FakeBackend()
    container = backend.client_obj.container
    container.objects.update(
        {
            "dir/": b"",
            "dir/a.txt": b"a",
            "dir/sub/b.txt": b"b",
            "other.txt": b"keep",
        }
    )
    _az("az://account/container/dir", backend).rm(recursive=True)
    assert container.objects == {"other.txt": b"keep"}
    assert container.list_calls == ["dir/"]
    assert container.bulk_delete_calls == [("dir/", "dir/a.txt", "dir/sub/b.txt")]


def test_rm_recursive_deletes_exact_object_before_prefix():
    backend = _FakeBackend()
    container = backend.client_obj.container
    container.objects.update(
        {
            "file.txt": b"x",
            "file.txt/nested.txt": b"keep",
        }
    )
    _az("az://account/container/file.txt", backend).rm(recursive=True)
    assert container.objects == {"file.txt/nested.txt": b"keep"}
    assert container.list_calls == []


def test_rm_recursive_missing_ok():
    _az("az://account/container/missing", _FakeBackend()).rm(
        recursive=True, missing_ok=True
    )


def test_rm_recursive_missing_without_missing_ok_raises():
    with pytest.raises(FileNotFoundError):
        _az("az://account/container/missing", _FakeBackend()).rm(recursive=True)


def test_rm_recursive_ignore_error_swallows_missing():
    calls = []
    _az("az://account/container/missing", _FakeBackend()).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append((type(err), path.key)) or True,
    )
    assert calls == [(FileNotFoundError, "missing")]


def test_rm_recursive_root_guard():
    backend = _FakeBackend()
    container = backend.client_obj.container
    container.objects["a.txt"] = b"x"
    with pytest.raises(PermissionError):
        _az("az://account/container/", backend).rm(recursive=True)
    assert container.objects == {"a.txt": b"x"}


def test_rm_recursive_delete_error_reroutes_to_ignore_error():
    backend = _FakeBackend()
    container = backend.client_obj.container
    container.objects["dir/a.txt"] = b"x"
    container.delete_errors.add("dir/a.txt")
    calls = []
    _az("az://account/container/dir", backend).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append((type(err), path.key)) or True,
    )
    assert calls == [(OSError, "dir/a.txt")]


# --- a trailing "/" names the directory; interior empty segments stay -------


@pytest.mark.parametrize(
    "uri, container, key",
    [
        ("az://account/container/dir/", "container", "dir"),
        ("az://account/container/dir", "container", "dir"),
        ("az://account/container/", "container", ""),
        ("az://account/container", "container", ""),
        # Literal blob-name bytes: this used to collapse to "a/b", leaving a
        # blob named "a//b" unreachable.
        ("az://account/container/a//b", "container", "a//b"),
        ("az://account/container/dir//", "container", "dir/"),
    ],
)
def test_container_and_key_drop_one_trailing_slash(uri, container, key):
    p = _az(uri)
    assert p.container == container
    assert p.key == key


def test_trailing_slash_marker_dir_is_a_directory_and_rm_deletes_tree():
    backend = _FakeBackend()
    container = backend.client_obj.container
    container.objects.update(
        {"dir/": b"", "dir/a.txt": b"a", "dir/sub/b.txt": b"b", "other.txt": b"k"}
    )
    p = _az("az://account/container/dir/", backend)
    assert p.is_dir()
    assert not p.is_file()
    p.rm(recursive=True)
    assert container.objects == {"other.txt": b"k"}


# --- only a not-found reply means "missing" ---------------------------------


def _container_with(bulk=True, **objects):
    backend = _FakeBackend(bulk=bulk)
    container = backend.client_obj.container
    container.objects.update(objects)
    return backend, container


def test_transient_properties_error_is_not_file_not_found():
    backend, container = _container_with(**{"report.csv": b"PRECIOUS"})
    container.properties_errors["report.csv"] = _ServiceUnavailable("503")
    p = _az("az://account/container/report.csv", backend)
    with pytest.raises(OSError) as info:
        p.stat()
    assert not isinstance(info.value, FileNotFoundError)
    assert info.value.__cause__ is None
    assert container.objects == {"report.csv": b"PRECIOUS"}


def test_rm_recursive_transient_properties_error_keeps_prefix_tree():
    backend, container = _container_with(**{"x": b"object", "x/child": b"c"})
    container.properties_errors["x"] = _ServiceUnavailable("503")
    with pytest.raises(OSError):
        _az("az://account/container/x", backend).rm(recursive=True)
    assert container.objects == {"x": b"object", "x/child": b"c"}


def test_unlink_under_lease_is_not_hidden_or_file_exists():
    backend, container = _container_with(**{"leased.txt": b"x"})
    container.delete_raises["leased.txt"] = _LeaseIdMissing("412 lease")
    p = _az("az://account/container/leased.txt", backend)
    for kwargs in ({"missing_ok": True}, {}):
        with pytest.raises(OSError) as info:
            p.unlink(**kwargs)
        assert type(info.value) is OSError
    assert "leased.txt" in container.objects


def test_missing_sdk_is_import_error_not_file_not_found():
    try:
        import azure.storage.blob  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("azure-storage-blob is installed")
    p = AzPath("az://account/container/a.txt")
    with pytest.raises(ImportError):
        p.read_bytes()


# --- the default backend uses the URI's account ------------------------------


def test_default_backend_targets_uri_account_without_identity(fake_blob_module):
    import sys

    sys.modules.pop("azure.identity", None)
    p = AzPath("az://myacct/cont/k")
    with pytest.raises(ImportError, match="azure-identity"):
        p.read_bytes()
    assert fake_blob_module == []


def test_default_backend_targets_uri_account_with_identity(
    fake_blob_module, monkeypatch
):
    import sys
    import types

    identity = types.ModuleType("azure.identity")

    class DefaultAzureCredential:
        pass

    identity.DefaultAzureCredential = DefaultAzureCredential
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    backend = AzPath("az://otheracct/cont/k").backend
    assert AzPath("az://otheracct/cont/j").backend is backend
    assert AzPath("az://thirdacct/cont/j").backend is not backend
    backend.client()
    ((kind, url, credential, kwargs),) = fake_blob_module
    assert (kind, url, kwargs) == (
        "init",
        "https://otheracct.blob.core.windows.net",
        {},
    )
    assert isinstance(credential, DefaultAzureCredential)


def test_connection_string_keeps_the_other_kwargs(fake_blob_module):
    from pathlib_next.uri.schemes.az import AzBackend

    AzBackend(
        connection_string="cs", credential="token", max_single_put_size=4
    ).client()
    assert fake_blob_module == [("conn", "cs", "token", {"max_single_put_size": 4})]


# --- missing or wrong-type targets raise like pathlib ------------------------


def test_iterdir_missing_and_file_raise(walk_sdk):
    backend, _container = _container_with(**{"file.txt": b"x", "d/": b""})
    with pytest.raises(FileNotFoundError):
        list(_az("az://account/container/typo", backend).iterdir())
    with pytest.raises(NotADirectoryError):
        list(_az("az://account/container/file.txt", backend).iterdir())
    assert list(_az("az://account/container/d", backend).iterdir()) == []


def test_unlink_directory_and_rmdir_wrong_targets():
    import errno

    backend, container = _container_with(**{"file.txt": b"x", "d/": b"", "d/f": b"y"})
    with pytest.raises(IsADirectoryError):
        _az("az://account/container/d", backend).unlink()
    with pytest.raises(FileNotFoundError):
        _az("az://account/container/nope", backend).rmdir()
    with pytest.raises(NotADirectoryError):
        _az("az://account/container/file.txt", backend).rmdir()
    with pytest.raises(OSError) as info:
        _az("az://account/container/d", backend).rmdir()
    assert info.value.errno == errno.ENOTEMPTY
    assert set(container.objects) == {"file.txt", "d/", "d/f"}


# --- write streams ----------------------------------------------------------


def test_failed_close_upload_is_not_retried_at_gc():
    import gc

    backend, container = _container_with()
    container.upload_errors.append(_ServiceUnavailable("503"))
    p = _az("az://account/container/state.json", backend)
    f = p.open("wb")
    f.write(b'{"version": 1}')
    with pytest.raises(OSError):
        f.close()
    assert f.closed
    p.write_bytes(b'{"version": 2}')
    del f
    gc.collect()
    assert container.objects["state.json"] == b'{"version": 2}'


def test_exclusive_create_is_atomic():
    backend, container = _container_with()
    first = _az("az://account/container/job.lock", backend).open("xb")
    second = _az("az://account/container/job.lock", backend).open("xb")
    first.write(b"worker1")
    second.write(b"worker2")
    first.close()
    with pytest.raises(FileExistsError):
        second.close()
    assert container.objects["job.lock"] == b"worker1"


def test_mkdir_race_is_file_exists():
    backend, container = _container_with()
    container.upload_errors.append(_Exists("409 BlobAlreadyExists"))
    with pytest.raises(FileExistsError):
        _az("az://account/container/d", backend).mkdir()


def test_rplus_writes_land_and_read_stream_is_read_only():
    import io

    backend, container = _container_with(**{"a.txt": b"hello"})
    p = _az("az://account/container/a.txt", backend)
    with p.open("rb") as f:
        with pytest.raises(io.UnsupportedOperation):
            f.write(b"x")
    with p.open("r+b") as f:
        assert f.read() == b"hello"
    assert container.uploads == []
    with p.open("r+b") as f:
        f.write(b"J")
    assert container.objects["a.txt"] == b"Jello"


# --- rename/move of a prefix directory ---------------------------------------


def test_rename_prefix_directory_falls_back_in_move(walk_sdk):
    backend, container = _container_with(**{"dir/a": b"a", "dir/sub/b": b"b"})
    src = _az("az://account/container/dir", backend)
    with pytest.raises(NotImplementedError):
        src.rename("dir2")
    with pytest.raises(FileNotFoundError):
        _az("az://account/container/nope", backend).rename("nope2")
    src.move(_az("az://account/container/dir3", backend))
    assert container.objects == {
        "dir3/": b"",
        "dir3/a": b"a",
        "dir3/sub/": b"",
        "dir3/sub/b": b"b",
    }


# --- bulk delete partial failure -----------------------------------------------


@pytest.mark.parametrize(
    "bulk, batch_rejected",
    [(True, False), (True, True), (False, False)],
    ids=["partial-batch", "rejected-batch", "no-bulk"],
)
def test_rm_recursive_one_failing_blob_does_not_leave_the_rest(bulk, batch_rejected):
    backend, container = _container_with(
        bulk=bulk, **{"dir/a.txt": b"a", "dir/b.txt": b"b", "dir/c.txt": b"c"}
    )
    container.batch_rejected = batch_rejected
    container.delete_errors.add("dir/a.txt")
    calls = []
    _az("az://account/container/dir", backend).rm(
        recursive=True,
        ignore_error=lambda err, path: calls.append(str(err)) or True,
    )
    assert container.objects == {"dir/a.txt": b"a"}
    assert calls == ["dir/a.txt"]


# --- objstore-listing-hides-object-prefix-collision -----------------------------


def test_key_that_is_object_and_prefix_lists_as_the_object(walk_sdk):
    backend, _container = _container_with(
        **{"src/logs": b"FILE-CONTENT", "src/logs/2026.txt": b"child", "src/d/x": b"x"}
    )
    listing = dict(_az("az://account/container/src", backend)._scandir())
    assert sorted(listing) == ["d", "logs"]
    assert not listing["logs"].is_dir()
    assert listing["d"].is_dir()
    assert not _az("az://account/container/src/logs", backend).stat().is_dir()


# --- objstore-gs-az-root-always-exists ------------------------------------------


def test_missing_container_root_does_not_exist():
    backend, container = _container_with()

    def missing(name_starts_with="", **_kwargs):
        raise _Missing("ContainerNotFound")

    container.list_blobs = missing
    root = _az("az://account/typo-container", backend)
    with pytest.raises(FileNotFoundError):
        root.stat()
    assert not root.exists()


def test_existing_container_root_is_a_directory_even_when_empty():
    backend, container = _container_with()
    assert _az("az://account/container/", backend).stat().is_dir()
    assert container.list_calls == [""]


def test_account_level_path_asks_for_containers():
    backend, _container = _container_with()
    calls = []

    def list_containers(**kwargs):
        calls.append(kwargs)
        raise _Missing("account not found")

    backend.client_obj.list_containers = list_containers
    account = _az("az://account/container/x", backend).parents[-1]
    assert account.container == ""
    with pytest.raises(FileNotFoundError):
        account.stat()
    assert not account.exists()
    assert calls

    backend.client_obj.list_containers = lambda **kwargs: iter(["container"])
    assert account.is_dir()


# --- objstore-az-rm-batch-fallback-aborts (regression) ---------------------------


def test_rejected_batch_falls_back_per_blob_with_each_blobs_error():
    backend, container = _container_with(**{f"dir/f{i}.txt": b"x" for i in range(5)})
    container.batch_rejected = True
    container.delete_raises["dir/f0.txt"] = _LeaseIdMissing("lease held")
    seen = []
    _az("az://account/container/dir", backend).rm(
        recursive=True,
        ignore_error=lambda err, path: seen.append(type(err).__name__) or True,
    )
    assert sorted(container.objects) == ["dir/f0.txt"]
    assert seen == ["OSError"]


# --- a listed name is one component inside the directory that listed it ------


def test_listing_skips_names_that_are_not_one_component(walk_sdk):
    backend, _container = _container_with(
        **{
            "dir/ok.txt": b"x",
            "dir/..": b"x",
            "dir/.": b"x",
            "dir/../up/f": b"x",
            "dir/./down/f": b"x",
            "dir/sub/f": b"x",
        }
    )
    listing = dict(_az("az://account/container/dir", backend)._scandir())
    assert sorted(listing) == ["ok.txt", "sub"]
    assert listing["sub"].is_dir()


def test_listing_skips_names_a_server_reports_with_a_separator(walk_sdk):
    from azure.storage.blob import BlobPrefix

    backend, container = _container_with(**{"dir/ok.txt": b"x", "dir/a/b": b"x"})

    def raw(name_starts_with="", delimiter="/"):
        yield _FakeBlobItem("dir/ok.txt", b"x")
        yield _FakeBlobItem("dir/a/b", b"x")
        for prefix in ("dir/sub/", "dir/p/q/", "dir//"):
            yield BlobPrefix(prefix=prefix)

    container.walk_blobs = raw
    listing = dict(_az("az://account/container/dir", backend)._scandir())
    assert sorted(listing) == ["ok.txt", "sub"]


# --- a prefix is moved, copied and removed as the same set of blobs ----------

_ODD_PREFIXES = {
    "an-empty-segment": {"d/x.txt": b"x", "d//y.txt": b"y", "d/sub//z.txt": b"z"},
    "a-blob-that-is-also-a-prefix": {
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
    backend, container = _container_with(**_ODD_PREFIXES[request.param])
    container.objects["unrelated.txt"] = b"keep"
    return backend, container, dict(container.objects)


def test_rm_recursive_of_a_prefix_with_hidden_blobs_removes_none(odd_prefix):
    backend, container, before = odd_prefix
    with pytest.raises(OSError, match="nothing was changed"):
        _az("az://account/container/d", backend).rm(recursive=True)
    assert container.objects == before
    assert container.deleted == []


def test_copy_recursive_of_a_prefix_with_hidden_blobs_is_refused(odd_prefix):
    from pathlib_next.mempath import MemPath

    backend, container, before = odd_prefix
    target = MemPath("/copied")
    with pytest.raises(OSError, match="nothing was changed"):
        _az("az://account/container/d", backend).copy(target, recursive=True)
    assert not target.exists()
    assert container.objects == before


def test_move_of_a_prefix_with_hidden_blobs_keeps_every_blob(odd_prefix):
    backend, container, before = odd_prefix
    with pytest.raises(OSError, match="nothing was changed"):
        _az("az://account/container/d", backend).move(
            _az("az://account/container/moved", backend)
        )
    assert container.objects == before


def test_the_refusal_names_the_blobs_a_listing_does_not_show():
    backend, container = _container_with(
        **{"d/x.txt": b"x", "d//y.txt": b"y", "d/logs": b"l", "d/logs/2026.txt": b"c"}
    )
    with pytest.raises(OSError) as raised:
        _az("az://account/container/d", backend).rm(recursive=True)
    assert "'d//y.txt'" in str(raised.value)
    assert "'d/logs/2026.txt'" in str(raised.value)
    assert "'d/x.txt'" not in str(raised.value)


def test_rm_recursive_with_ignore_error_removes_only_the_blobs_a_listing_shows():
    backend, container = _container_with(
        **{
            "d/": b"",
            "d/x.txt": b"x",
            "d//y.txt": b"y",
            "d/logs": b"l",
            "d/logs/z": b"z",
        }
    )
    seen = []
    _az("az://account/container/d", backend).rm(
        recursive=True, ignore_error=lambda err, path: seen.append(err) or True
    )
    assert len(seen) == 1 and "'d//y.txt'" in str(seen[0])
    assert sorted(container.objects) == ["d//y.txt", "d/logs/z"]


def test_move_of_a_prefix_a_listing_shows_moves_every_blob(walk_sdk):
    backend, container = _container_with(
        **{"d/": b"", "d/x.txt": b"x", "d/sub/a.txt": b"a", "d/empty/": b""}
    )
    _az("az://account/container/d", backend).move(
        _az("az://account/container/moved", backend)
    )
    assert sorted(container.objects) == [
        "moved/",
        "moved/empty/",
        "moved/sub/",
        "moved/sub/a.txt",
        "moved/x.txt",
    ]


def test_rm_recursive_of_a_prefix_a_listing_shows_is_one_batched_delete():
    backend, container = _container_with(
        **{"d/": b"", "d/x.txt": b"x", "d/sub/a.txt": b"a", "d/empty/": b"", "k": b""}
    )
    _az("az://account/container/d", backend).rm(recursive=True)
    assert container.bulk_delete_calls == [("d/", "d/empty/", "d/sub/a.txt", "d/x.txt")]
    assert container.objects == {"k": b""}


# --- a key that is both a blob and a prefix lists as the blob ----------------


def test_blob_wins_over_a_prefix_of_the_same_name_whatever_the_listing_order(walk_sdk):
    backend, container = _container_with(
        **{"src/logs": b"FILE-CONTENT", "src/logs/2026.txt": b"child", "src/d/x": b"x"}
    )
    names = [
        item.name
        for item in container.walk_blobs(name_starts_with="src/", delimiter="/")
    ]
    # The SDK puts every prefix of a page before its blobs.
    assert names == ["src/d/", "src/logs/", "src/logs"]
    listing = dict(_az("az://account/container/src", backend)._scandir())
    assert sorted(listing) == ["d", "logs"]
    assert not listing["logs"].is_dir()
    assert listing["logs"].st_size == len(b"FILE-CONTENT")


# --- a write or rename never turns a prefix directory into a blob -------------


def test_write_onto_a_prefix_directory_is_refused():
    backend, container = _container_with(**{"d/": b"", "d/x.txt": b"x"})
    with pytest.raises(IsADirectoryError):
        _az("az://account/container/d", backend).write_bytes(b"clobber")
    assert container.objects == {"d/": b"", "d/x.txt": b"x"}
    assert container.uploads == []


def test_rename_onto_a_prefix_directory_is_refused():
    backend, container = _container_with(**{"b.txt": b"b", "dst/keep.txt": b"k"})
    with pytest.raises(IsADirectoryError):
        _az("az://account/container/b.txt", backend).rename("dst")
    assert container.objects == {"b.txt": b"b", "dst/keep.txt": b"k"}


def test_write_and_rename_onto_a_blob_still_replace_it():
    backend, container = _container_with(**{"a.txt": b"a", "b.txt": b"b"})
    _az("az://account/container/a.txt", backend).write_bytes(b"new")
    assert container.objects["a.txt"] == b"new"
    _az("az://account/container/b.txt", backend).rename("a.txt")
    assert container.objects == {"a.txt": b"b"}


def test_write_where_the_prefix_cannot_be_listed_still_writes():
    backend, container = _container_with()

    def denied(name_starts_with="", **_kwargs):
        raise _Forbidden("AuthorizationFailure")

    container.list_blobs = denied
    _az("az://account/container/w.txt", backend).write_bytes(b"written")
    assert container.objects == {"w.txt": b"written"}


# --- mkdir treats only "not found" as absent -----------------------------------


def test_mkdir_does_not_create_a_marker_when_the_probe_fails():
    backend, container = _container_with(**{"existing.txt": b"keep"})
    container.properties_errors["existing.txt"] = _ServiceUnavailable("503")
    with pytest.raises(OSError) as raised:
        _az("az://account/container/existing.txt", backend).mkdir()
    assert not isinstance(raised.value, FileExistsError)
    assert container.objects == {"existing.txt": b"keep"}
    assert container.uploads == []


def test_mkdir_on_a_blob_is_file_exists_and_on_a_missing_path_writes_the_marker():
    backend, container = _container_with(**{"existing.txt": b"keep"})
    with pytest.raises(FileExistsError):
        _az("az://account/container/existing.txt", backend).mkdir()
    _az("az://account/container/newdir", backend).mkdir()
    assert sorted(container.objects) == ["existing.txt", "newdir/"]


# --- removing a container is not supported --------------------------------------


def test_rmdir_of_the_container_root_is_refused():
    backend, container = _container_with(**{"a.txt": b"x"})
    root = _az("az://account/container/", backend)
    with pytest.raises(PermissionError):
        root.rmdir()
    with pytest.raises(PermissionError):
        root.rm()
    assert container.objects == {"a.txt": b"x"}
    assert container.deleted == []


# --- a failed request is an OSError of the right type, with nothing chained --


class _GatewayTimeout(_HttpResponseError):
    status_code = 504


def _properties_failure(error):
    backend, container = _container_with(**{"a.txt": b"x"})
    container.properties_errors["a.txt"] = error
    return _az("az://account/container/a.txt", backend)


def _azure_core():
    return pytest.importorskip("azure.core.exceptions")


def test_a_connection_failure_is_a_connection_error_naming_the_path():
    azexc = _azure_core()
    error = azexc.ServiceRequestError("refused: /c/a.txt?sv=1&sig=SECRET")
    path = _properties_failure(error)
    with pytest.raises(ConnectionError) as info:
        path.stat()
    assert not isinstance(info.value, (TimeoutError, ConnectionResetError))
    assert info.value.filename == "az://account/container/a.txt"
    assert info.value.__cause__ is None
    assert "SECRET" not in str(info.value)
    assert path.exists() is False
    backend, container = _container_with(**{"a.txt": b"x"})

    def refused(*args, **kwargs):
        raise error

    container.walk_blobs = refused
    errors = []
    root = _az("az://account/container", backend)
    assert list(root.walk(on_error=errors.append)) == []
    assert [type(e) for e in errors] == [ConnectionError]


def test_a_request_that_times_out_is_a_timeout_error():
    azexc = _azure_core()
    requests_exc = pytest.importorskip("requests.exceptions")
    errors = [
        azexc.ServiceRequestError("slow", error=requests_exc.ConnectTimeout("c")),
        azexc.ServiceResponseError("slow", error=requests_exc.ReadTimeout("r")),
    ]
    for name in ("ServiceRequestTimeoutError", "ServiceResponseTimeoutError"):
        if hasattr(azexc, name):
            errors.append(getattr(azexc, name)("slow"))
    for error in errors:
        path = _properties_failure(error)
        with pytest.raises(TimeoutError) as info:
            path.stat()
        assert info.value.filename == "az://account/container/a.txt"
        assert info.value.__cause__ is None


def test_a_gateway_timeout_reply_is_a_timeout_error():
    path = _properties_failure(_GatewayTimeout("504"))
    with pytest.raises(TimeoutError):
        path.stat()


def test_a_response_that_does_not_arrive_whole_is_a_connection_reset():
    azexc = _azure_core()
    errors = [azexc.ServiceResponseError("Connection aborted")]
    if hasattr(azexc, "IncompleteReadError"):
        errors.append(azexc.IncompleteReadError("short body"))
    for error in errors:
        path = _properties_failure(error)
        with pytest.raises(ConnectionResetError) as info:
            path.stat()
        assert info.value.filename == "az://account/container/a.txt"
        assert info.value.__cause__ is None


def test_a_body_cut_short_is_a_connection_reset_naming_the_path(monkeypatch):
    azexc = _azure_core()
    backend, container = _container_with(**{"a.txt": b"x"})

    def cut(self):
        raise azexc.ServiceResponseError("Connection broken")

    monkeypatch.setattr(_FakeDownloader, "readall", cut)
    with pytest.raises(ConnectionResetError) as info:
        _az("az://account/container/a.txt", backend).read_bytes()
    assert info.value.filename == "az://account/container/a.txt"
    assert info.value.__cause__ is None


def test_other_exceptions_still_propagate_as_themselves():
    path = _properties_failure(KeyError("a bug"))
    with pytest.raises(KeyError):
        path.stat()


# --- paths that name no blob ---------------------------------------------------


def test_a_container_root_is_a_directory_to_read_or_write():
    backend, container = _container_with(**{"a.txt": b"x"})
    root = _az("az://account/container/", backend)
    with pytest.raises(IsADirectoryError):
        root.read_bytes()
    with pytest.raises(IsADirectoryError):
        root.write_bytes(b"clobber")
    with pytest.raises(IsADirectoryError):
        root.open("r+b")
    assert container.list_calls == [] and container.uploads == []
    assert container.objects == {"a.txt": b"x"}


def test_an_account_lists_its_containers_as_directories(walk_sdk):
    backend, _container = _container_with()

    class Properties:
        def __init__(self, name):
            self.name = name

    backend.client_obj.list_containers = lambda **kwargs: iter(
        [Properties("logs"), Properties("data")]
    )
    account = _az("az://account/container/x", backend).parents[-1]
    assert account.container == ""
    assert account.is_dir()
    children = {child.name: child.is_dir() for child in account.iterdir()}
    assert children == {"logs": True, "data": True}
    with pytest.raises(IsADirectoryError):
        account.read_bytes()
    with pytest.raises(IsADirectoryError):
        account.write_bytes(b"x")


def test_a_path_without_an_account_does_not_exist_and_builds_no_credential(
    monkeypatch,
):
    from pathlib_next.uri.schemes import az

    def forbidden(*args, **kwargs):
        raise AssertionError("a credential must not be built")

    monkeypatch.setattr(az, "_default_credential", forbidden)
    monkeypatch.setattr(az, "_DEFAULT_BACKENDS", {})
    for uri in ("az:x", "az:///c/x"):
        path = AzPath(uri)
        assert path.exists() is False
        with pytest.raises(FileNotFoundError):
            path.stat()
        with pytest.raises(FileNotFoundError):
            path.write_bytes(b"x")
    assert az._DEFAULT_BACKENDS == {}


def test_removing_a_missing_container_with_missing_ok_is_not_an_error():
    backend, container = _container_with()

    def missing(name_starts_with="", **_kwargs):
        raise _Missing("ContainerNotFound")

    container.list_blobs = missing
    path = _az("az://account/nocontainer/x", backend)
    assert path.rm(recursive=True, missing_ok=True) is None
    with pytest.raises(FileNotFoundError):
        path.rm(recursive=True)
    offered = []
    path.rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append(type(error)) or True,
    )
    assert offered == [FileNotFoundError]


def test_renaming_a_missing_path_onto_its_own_name_is_file_not_found():
    backend, container = _container_with(**{"there.txt": b"x", "d/f": b"x"})
    with pytest.raises(FileNotFoundError):
        _az("az://account/container/nope", backend).rename("nope")
    there = _az("az://account/container/there.txt", backend)
    assert there.rename("there.txt") == there
    assert _az("az://account/container/d", backend).rename("d").key == "d"
    assert container.objects == {"there.txt": b"x", "d/f": b"x"}
