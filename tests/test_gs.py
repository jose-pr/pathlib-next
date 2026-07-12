import pytest

pytest.importorskip("google.cloud.storage")

from pathlib_next.uri.schemes.gs import GsPath, GsBackend
from pathlib_next.uri import UriPath


def test_gs_path_registration():
    """Test that gs: scheme resolves to GsPath."""
    path = UriPath("gs://bucket/key/path", findclass=True)
    assert isinstance(path, GsPath)


def test_gs_path_components(gs_server):
    """Test that GsPath components are parsed correctly."""
    path, _ = gs_server
    assert path.bucket_name == "test-bucket"
    assert path.key == ""


def test_gs_stat_root(gs_server):
    """Root always exists and is a directory."""
    path, _ = gs_server
    stat = path.stat()
    assert stat.is_dir()


def test_gs_list_empty(gs_server):
    """List on empty bucket."""
    path, _ = gs_server
    children = list(path.iterdir())
    assert len(children) > 0  # fixture_tree has files


def test_gs_read_existing_file(gs_server, fixture_tree):
    """Read an existing file from the bucket."""
    path, backend = gs_server
    file_content = (fixture_tree / "a.txt").read_bytes()
    file_path = path / "a.txt"
    content = file_path.read_bytes()
    assert content == file_content


def test_gs_write_new_file(gs_server):
    """Write a new file to the bucket."""
    path, backend = gs_server
    test_path = path / "new_file.txt"
    test_path.write_bytes(b"test content")
    content = test_path.read_bytes()
    assert content == b"test content"


def test_gs_mkdir(gs_server):
    """Create a directory marker."""
    path, backend = gs_server
    dir_path = path / "testdir"
    dir_path.mkdir()
    assert dir_path.exists()
    assert dir_path.is_dir()


def test_gs_delete_file(gs_server):
    """Delete a file."""
    path, backend = gs_server
    test_path = path / "to_delete.txt"
    test_path.write_bytes(b"delete me")
    assert test_path.exists()
    test_path.unlink()
    assert not test_path.exists()


def test_gs_open_modes(gs_server):
    """Test open() with different modes."""
    path, backend = gs_server
    test_path = path / "mode_test.txt"

    # Write mode
    with test_path.open("wb") as f:
        f.write(b"hello")

    # Read mode
    with test_path.open("rb") as f:
        content = f.read()
    assert content == b"hello"

    # Exclusive mode (should fail if exists)
    with pytest.raises(FileExistsError):
        test_path.open("x")
