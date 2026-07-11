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
    # Confirm it actually landed on disk, independent of our own backend
    # registry/cache (see registry tests below for cross-instance sharing).
    with zipfile.ZipFile(zip_archive) as zf:
        assert zf.read("new.txt") == b"new content"
        assert sorted(zf.namelist()) == ["docs/readme.txt", "new.txt", "top.txt"]


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


# --- backend registry: independently-constructed instances share one handle ---


def test_independently_constructed_instances_share_one_backend(zip_archive):
    a = UriPath(_zip_uri(zip_archive))
    b = UriPath(_zip_uri(zip_archive))
    assert a.backend is b.backend


def test_write_via_one_instance_is_immediately_visible_via_another(zip_archive):
    a = UriPath(_zip_uri(zip_archive))
    b = UriPath(_zip_uri(zip_archive))
    (a / "new.txt").write_text("written via a")
    assert (b / "new.txt").read_text() == "written via a"


def test_different_archives_get_different_backends(zip_archive, tmp_path):
    other = tmp_path / "b.zip"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("x.txt", "x")
    a = UriPath(_zip_uri(zip_archive))
    b = UriPath(_zip_uri(other))
    assert a.backend is not b.backend


# --- zip mutations: unlink / rmdir / rename / overwrite ---


def test_zip_unlink_removes_entry(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    (root / "top.txt").unlink()
    assert not (root / "top.txt").exists()
    with zipfile.ZipFile(zip_archive) as zf:
        assert "top.txt" not in zf.namelist()
        assert "docs/readme.txt" in zf.namelist()  # untouched


def test_zip_unlink_missing_raises_file_not_found(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    with pytest.raises(FileNotFoundError):
        (root / "nope.txt").unlink()


def test_zip_unlink_missing_ok(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    (root / "nope.txt").unlink(missing_ok=True)  # no raise


def test_zip_rmdir_removes_empty_dir_marker(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    (root / "empty").mkdir()
    (root / "empty").rmdir()
    assert not (root / "empty").exists()
    with zipfile.ZipFile(zip_archive) as zf:
        assert "empty/" not in zf.namelist()


def test_zip_rmdir_nonempty_raises_os_error(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    with pytest.raises(OSError):
        (root / "docs").rmdir()
    assert (root / "docs" / "readme.txt").exists()  # untouched


def test_zip_rmdir_missing_raises_file_not_found(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    with pytest.raises(FileNotFoundError):
        (root / "nosuchdir").rmdir()


def test_zip_rename_file_entry(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    (root / "top.txt").rename("renamed.txt")
    assert not (root / "top.txt").exists()
    assert (root / "renamed.txt").read_text() == "top level"


def test_zip_rename_directory_moves_nested_entries(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    (root / "docs").rename(root / "moved")
    assert not (root / "docs").exists()
    assert (root / "moved").is_dir()
    assert (root / "moved" / "readme.txt").read_text() == "hello world"


def test_zip_overwrite_existing_entry_replaces_content_not_duplicates(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    (root / "top.txt").write_text("replaced content")
    assert (root / "top.txt").read_text() == "replaced content"
    with zipfile.ZipFile(zip_archive) as zf:
        assert zf.namelist().count("top.txt") == 1
        assert zf.read("top.txt") == b"replaced content"


def test_zip_mutations_require_local_outer_archive(zip_archive):
    b64 = base64.b64encode(zip_archive.read_bytes()).decode()
    p = UriPath(f"zip:data:application/zip;base64,{b64}!/top.txt")
    with pytest.raises(NotImplementedError):
        p.unlink()
    with pytest.raises(NotImplementedError):
        p.rename("x.txt")


def test_tar_has_no_mutation_support(tar_archive):
    p = UriPath(_tar_uri(tar_archive)) / "top.txt"
    with pytest.raises(NotImplementedError):
        p.unlink()
    with pytest.raises(NotImplementedError):
        p.rmdir()
    with pytest.raises(NotImplementedError):
        p.rename("x.txt")
