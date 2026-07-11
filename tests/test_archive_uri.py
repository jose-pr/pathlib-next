"""zip:/tar: archive paths: `<scheme>:<archive-uri>!/<inner-path>`
(Java-style separator). Uses real `zipfile`/`tarfile` archives on disk
(tmp_path) as the outer -- no mocking needed, these are stdlib and fast.
"""
import base64
import io
import tarfile
import zipfile

import pytest

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.archive import TarUri, ZipUri, _split_archive_path


# --- URI splitting ---


@pytest.mark.parametrize(
    "path, archive, inner",
    [
        ("file:///a.zip!/docs/readme.txt", "file:///a.zip", "docs/readme.txt"),
        ("file:///a.zip!/", "file:///a.zip", ""),
        ("file:///a.zip!", "file:///a.zip", ""),
        ("file:///a.zip", "file:///a.zip", ""),
    ],
)
def test_split_archive_path(path, archive, inner):
    assert _split_archive_path(path) == (archive, inner)


def test_archive_uri_without_scheme_raises_value_error():
    with pytest.raises(ValueError):
        UriPath("zip:/just/a/path!/x")


# --- fixtures: real zip/tar archives on disk ---


@pytest.fixture
def zip_archive(tmp_path):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("docs/readme.txt", "hello world")
        zf.writestr("top.txt", "top level")
    return path


@pytest.fixture
def tar_archive(tmp_path):
    path = tmp_path / "a.tar"
    with tarfile.open(path, "w") as tf:
        for name, data in [("docs/readme.txt", b"hello tar"), ("top.txt", b"top level")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def _zip_uri(path, inner=""):
    return f"zip:{path.as_uri()}!/{inner}"


def _tar_uri(path, inner=""):
    return f"tar:{path.as_uri()}!/{inner}"


# --- scheme dispatch ---


def test_zip_scheme_dispatch(zip_archive):
    p = UriPath(_zip_uri(zip_archive))
    assert isinstance(p, ZipUri)


def test_tar_scheme_dispatch(tar_archive):
    p = UriPath(_tar_uri(tar_archive))
    assert isinstance(p, TarUri)


# --- read: listdir, stat, name/parent, content ---


@pytest.mark.parametrize("uri_fn", [_zip_uri, _tar_uri])
def test_root_is_dir_and_lists_top_level(uri_fn, zip_archive, tar_archive):
    archive = zip_archive if uri_fn is _zip_uri else tar_archive
    root = UriPath(uri_fn(archive))
    assert root.is_dir()
    assert sorted(p.name for p in root.iterdir()) == ["docs", "top.txt"]


@pytest.mark.parametrize("uri_fn", [_zip_uri, _tar_uri])
def test_nested_dir_lists_its_children(uri_fn, zip_archive, tar_archive):
    archive = zip_archive if uri_fn is _zip_uri else tar_archive
    docs = UriPath(uri_fn(archive)) / "docs"
    assert docs.is_dir()
    assert [p.name for p in docs.iterdir()] == ["readme.txt"]


@pytest.mark.parametrize("uri_fn", [_zip_uri, _tar_uri])
def test_read_member_content(uri_fn, zip_archive, tar_archive):
    archive = zip_archive if uri_fn is _zip_uri else tar_archive
    readme = UriPath(uri_fn(archive)) / "docs" / "readme.txt"
    assert readme.is_file()
    assert not readme.is_dir()
    assert readme.exists()


def test_zip_read_text(zip_archive):
    readme = UriPath(_zip_uri(zip_archive)) / "docs" / "readme.txt"
    assert readme.read_text() == "hello world"
    assert readme.stat().st_size == 11


def test_tar_read_bytes(tar_archive):
    readme = UriPath(_tar_uri(tar_archive)) / "docs" / "readme.txt"
    assert readme.read_bytes() == b"hello tar"
    assert readme.stat().st_size == 9


@pytest.mark.parametrize("uri_fn", [_zip_uri, _tar_uri])
def test_missing_member_not_found(uri_fn, zip_archive, tar_archive):
    archive = zip_archive if uri_fn is _zip_uri else tar_archive
    missing = UriPath(uri_fn(archive)) / "nope.txt"
    assert not missing.exists()
    with pytest.raises(FileNotFoundError):
        missing.read_bytes()


@pytest.mark.parametrize("uri_fn", [_zip_uri, _tar_uri])
def test_name_and_parent(uri_fn, zip_archive, tar_archive):
    archive = zip_archive if uri_fn is _zip_uri else tar_archive
    readme = UriPath(uri_fn(archive)) / "docs" / "readme.txt"
    assert readme.name == "readme.txt"
    assert readme.parent.name == "docs"


def test_as_uri_round_trips_zip(zip_archive):
    readme = UriPath(_zip_uri(zip_archive)) / "docs" / "readme.txt"
    round_tripped = UriPath(readme.as_uri())
    assert round_tripped.read_text() == "hello world"


# --- zip write: local outer archive ---


def test_zip_write_new_entry_to_local_archive(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    (root / "new.txt").write_text("new content")
    # Reopen fresh (separate backend/handle) to confirm it was persisted.
    reopened = UriPath(_zip_uri(zip_archive))
    assert (reopened / "new.txt").read_text() == "new content"
    assert sorted(p.name for p in reopened.iterdir()) == ["docs", "new.txt", "top.txt"]


def test_zip_mkdir_local_archive(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    (root / "sub").mkdir()
    reopened = UriPath(_zip_uri(zip_archive))
    assert (reopened / "sub").is_dir()


def test_zip_mkdir_existing_raises_file_exists(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    with pytest.raises(FileExistsError):
        (root / "docs").mkdir()


def test_zip_open_x_mode_existing_raises_file_exists(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    with pytest.raises(FileExistsError):
        (root / "top.txt").open("x")


# --- zip write: non-local outer archive is read-only ---


def test_zip_embedded_in_data_uri_is_read_only(zip_archive):
    b64 = base64.b64encode(zip_archive.read_bytes()).decode()
    p = UriPath(f"zip:data:application/zip;base64,{b64}!/docs/readme.txt")
    assert p.read_text() == "hello world"
    with pytest.raises(NotImplementedError):
        p.write_text("x")


# --- tar is always read-only ---


def test_tar_write_raises_not_implemented(tar_archive):
    p = UriPath(_tar_uri(tar_archive)) / "new.txt"
    with pytest.raises(NotImplementedError):
        p.write_text("x")


def test_tar_mkdir_raises_not_implemented(tar_archive):
    p = UriPath(_tar_uri(tar_archive)) / "newdir"
    with pytest.raises(NotImplementedError):
        p.mkdir()
