import datetime

import pytest

from pathlib_next.uri.schemes.az import AzPath, BaseAzBackend


class _Missing(Exception):
    pass


class _FakeBlobItem:
    def __init__(self, name, data):
        self.name = name
        self.size = len(data)
        self.last_modified = datetime.datetime(2026, 1, 1, 12, 0, 0)


class _FakeBlobClient:
    def __init__(self, container, name):
        self._container = container
        self.name = name
        self.url = f"https://example.invalid/{name}"

    def get_blob_properties(self):
        if self.name not in self._container.objects:
            raise _Missing(self.name)
        return {
            "size": len(self._container.objects[self.name]),
            "last_modified": datetime.datetime(2026, 1, 1, 12, 0, 0),
        }

    def delete_blob(self):
        if self.name in self._container.delete_errors:
            raise OSError(self.name)
        if self.name not in self._container.objects:
            raise _Missing(self.name)
        del self._container.objects[self.name]
        self._container.deleted.append(self.name)

    def upload_blob(self, data, overwrite=False):
        self._container.objects[self.name] = data


class _FakeContainer:
    def __init__(self, *, bulk=True):
        self.objects = {}
        self.deleted = []
        self.delete_errors = set()
        self.list_calls = []
        self.bulk_delete_calls = []
        if not bulk:
            self.delete_blobs = None

    def get_blob_client(self, name):
        return _FakeBlobClient(self, name)

    def list_blobs(self, name_starts_with=""):
        self.list_calls.append(name_starts_with)
        for name in sorted(self.objects):
            if name.startswith(name_starts_with):
                yield _FakeBlobItem(name, self.objects[name])

    def delete_blobs(self, *names):
        self.bulk_delete_calls.append(names)
        for name in names:
            if name in self.delete_errors:
                raise OSError(name)
            if name in self.objects:
                del self.objects[name]
                self.deleted.append(name)


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
    assert calls == [(OSError, "dir")]
