import pytest

pytest.importorskip("azure.storage.blob")

from pathlib_next.uri.schemes.az import AzPath, AzBackend
from pathlib_next.uri import UriPath


def test_az_path_registration():
    """Test that az: scheme resolves to AzPath."""
    path = UriPath("az://account/container/key/path", findclass=True)
    assert isinstance(path, AzPath)


def test_az_path_components(az_server):
    """Test that AzPath components are parsed correctly."""
    path, _ = az_server
    assert path.account == "testaccount"
    assert path.container == "testcontainer"
    assert path.key == ""


def test_az_stat_root(az_server):
    """Root always exists and is a directory."""
    path, _ = az_server
    stat = path.stat()
    assert stat.is_dir()


def test_az_list_empty(az_server):
    """List on container with files."""
    path, _ = az_server
    children = list(path.iterdir())
    assert len(children) > 0  # fixture_tree has files


def test_az_read_existing_file(az_server, fixture_tree):
    """Read an existing file from the container."""
    path, backend = az_server
    file_content = (fixture_tree / "a.txt").read_bytes()
    file_path = path / "a.txt"
    content = file_path.read_bytes()
    assert content == file_content


def test_az_write_new_file(az_server):
    """Write a new file to the container."""
    path, backend = az_server
    test_path = path / "new_file.txt"
    test_path.write_bytes(b"test content")
    content = test_path.read_bytes()
    assert content == b"test content"


def test_az_mkdir(az_server):
    """Create a directory marker."""
    path, backend = az_server
    dir_path = path / "testdir"
    dir_path.mkdir()
    assert dir_path.exists()
    assert dir_path.is_dir()


def test_az_delete_file(az_server):
    """Delete a file."""
    path, backend = az_server
    test_path = path / "to_delete.txt"
    test_path.write_bytes(b"delete me")
    assert test_path.exists()
    test_path.unlink()
    assert not test_path.exists()


def test_az_open_modes(az_server):
    """Test open() with different modes."""
    path, backend = az_server
    test_path = path / "mode_test.txt"

    # Write mode
    with test_path.open("w") as f:
        f.write(b"hello")

    # Read mode
    with test_path.open("r") as f:
        content = f.read()
    assert content == b"hello"

    # Exclusive mode (should fail if exists)
    with pytest.raises(FileExistsError):
        test_path.open("x")
