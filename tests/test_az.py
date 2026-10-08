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


def test_az_list_container_root(az_server):
    """List the container root, which holds fixture_tree."""
    path, _ = az_server
    children = {child.name for child in path.iterdir()}
    assert children == {"a.txt", "b.py", ".hidden.txt", "sub", "empty_dir"}


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
    with test_path.open("wb") as f:
        f.write(b"hello")

    # Read mode
    with test_path.open("rb") as f:
        content = f.read()
    assert content == b"hello"

    # Exclusive mode (should fail if exists)
    with pytest.raises(FileExistsError):
        test_path.open("x")


def _blob_that_is_also_a_prefix(path):
    base = path / "col"
    (base / "logs").write_bytes(b"FILE-CONTENT")
    (base / "logs" / "2026.txt").write_bytes(b"child")
    (base / "d" / "x").write_bytes(b"x")
    return base


def test_az_blob_that_is_also_a_prefix_lists_as_the_blob(az_server):
    """The SDK lists a page's prefixes before its blobs; the blob still wins."""
    path, _ = az_server
    base = _blob_that_is_also_a_prefix(path)
    listing = dict(base._scandir())
    assert sorted(listing) == ["d", "logs"]
    assert not listing["logs"].is_dir()
    assert listing["logs"].st_size == len(b"FILE-CONTENT")
    assert not (base / "logs").stat().is_dir()


def test_az_prefix_with_a_hidden_subtree_is_not_copied_moved_or_removed(az_server):
    from pathlib_next.mempath import MemPath

    path, _ = az_server
    base = _blob_that_is_also_a_prefix(path)
    target = MemPath("/copied")
    with pytest.raises(OSError, match="nothing was changed"):
        base.copy(target, recursive=True)
    assert not target.exists()
    with pytest.raises(OSError, match="nothing was changed"):
        base.move(path / "moved")
    with pytest.raises(OSError, match="nothing was changed"):
        base.rm(recursive=True)
    assert (base / "logs" / "2026.txt").read_bytes() == b"child"
    assert (base / "d" / "x").read_bytes() == b"x"
    assert not (path / "moved").exists()


def test_az_write_onto_a_prefix_directory_is_refused(az_server):
    path, _ = az_server
    with pytest.raises(IsADirectoryError):
        (path / "sub").write_bytes(b"clobber")
    assert (path / "sub").is_dir()


def test_az_rmdir_of_the_container_root_is_refused(az_server):
    path, _ = az_server
    with pytest.raises(PermissionError):
        path.rmdir()
    assert (path / "a.txt").exists()


def _closed_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_az_nothing_listening_is_a_connection_error_with_the_sas_token_unseen():
    # The SDK's own error is not an OSError, and its text names the URL.
    sas = "sv=2024-01-01&sp=r&sig=SECRETSIGNATUREVALUE"
    backend = AzBackend(
        account_url=f"http://127.0.0.1:{_closed_port()}/acct?{sas}",
        retry_total=0,
        # A refused loopback connection takes about two seconds to be
        # reported on Windows; a shorter timeout would be the error instead.
        connection_timeout=10,
    )
    path = AzPath("az://acct/container/blob.txt", backend=backend)
    with pytest.raises(ConnectionError) as info:
        path.stat()
    assert not isinstance(info.value, TimeoutError)
    assert info.value.filename == "az://acct/container/blob.txt"
    assert info.value.__cause__ is None
    assert "SECRETSIGNATUREVALUE" not in str(info.value)
    assert path.exists() is False
