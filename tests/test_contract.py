"""Filesystem contract shared by every Path implementation.

This is the reusable "one test class, many backends" pattern the project
wants third-party implementers (custom Path subclasses like MemPath, or
UriPath schemes) to be able to run against their own implementation -- see
AGENTS.md's Track A/Track B.
"""
import os
import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.testing import PurePathContract, ReadPathContract, PathContract
from pathlib_next.uri.schemes.file import FileUri
from pathlib_next.uri.schemes.data import DataUri
from pathlib_next.uri.schemes.archive import ZipUri, TarUri


def populate_mempath_from_local(local_path, mem_path):
    for entry in local_path.iterdir():
        mem_child = mem_path / entry.name
        if entry.is_dir():
            mem_child.mkdir()
            populate_mempath_from_local(entry, mem_child)
        else:
            mem_child.write_bytes(entry.read_bytes())


def make_zip_from_local(local_dir, zip_path):
    import zipfile
    with zipfile.ZipFile(zip_path, "w") as zf:
        for root, dirs, files in os.walk(local_dir):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, local_dir).replace("\\", "/")
                zf.write(full_path, rel_path)
            for d in dirs:
                full_path = os.path.join(root, d)
                if not os.listdir(full_path):
                    rel_path = os.path.relpath(full_path, local_dir).replace("\\", "/") + "/"
                    zf.writestr(rel_path, "")


def make_tar_from_local(local_dir, tar_path):
    import tarfile
    with tarfile.open(tar_path, "w") as tf:
        for root, dirs, files in os.walk(local_dir):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, local_dir).replace("\\", "/")
                tf.add(full_path, arcname=rel_path)
            for d in dirs:
                full_path = os.path.join(root, d)
                if not os.listdir(full_path):
                    rel_path = os.path.relpath(full_path, local_dir).replace("\\", "/")
                    tf.add(full_path, arcname=rel_path)


# Writable, full RW contract backends
BACKENDS = ["local", "mem", "fileuri"]


class TestWritableContracts(PathContract):
    @pytest.fixture(params=BACKENDS)
    def root(self, request, fixture_tree):
        if request.param == "local":
            return pathlib_next.LocalPath(fixture_tree)
        if request.param == "mem":
            m = MemPath("/")
            populate_mempath_from_local(fixture_tree, m)
            return m
        if request.param == "fileuri":
            return FileUri(fixture_tree.as_uri())
        raise AssertionError(request.param)


# HTTP read-only contract
class TestHttpContract(ReadPathContract):
    @pytest.fixture
    def root(self, http_server):
        from pathlib_next.uri.schemes.http import HttpPath
        return HttpPath(http_server)


# Zip read/write contract (local outer archive -- full mutation support:
# unlink/rmdir/rename/overwrite, see polish_perf/09)
class TestZipContract(PathContract):
    @pytest.fixture
    def root(self, tmp_path, fixture_tree):
        zip_file = tmp_path / "tree.zip"
        make_zip_from_local(fixture_tree, zip_file)
        return ZipUri(f"zip:{zip_file.as_uri()}!/")


# Tar read-only contract
class TestTarContract(ReadPathContract):
    @pytest.fixture
    def root(self, tmp_path, fixture_tree):
        tar_file = tmp_path / "tree.tar"
        make_tar_from_local(fixture_tree, tar_file)
        return TarUri(f"tar:{tar_file.as_uri()}!/")


# DataUri read-only contract (represents single file)
class TestDataUriContract(ReadPathContract):
    @pytest.fixture
    def root(self):
        return DataUri("data:text/plain,a")

    def test_exists_and_types(self, root):
        assert root.exists()
        assert root.is_file()
        assert not root.is_dir()

    def test_read_text_and_bytes(self, root):
        assert root.read_text() == "a"
        assert root.read_bytes() == b"a"

    def test_iterdir_lists_children(self, root):
        with pytest.raises(NotADirectoryError):
            list(root.iterdir())

    def test_stat(self, root):
        st = root.stat()
        assert st.st_size == 1


# Ftp contract
class TestFtpContract(PathContract):
    @pytest.fixture
    def root(self, ftp_server):
        from pathlib_next.uri.schemes.ftp import FtpPath
        return FtpPath(ftp_server)


# WebDAV contract
class TestDavContract(PathContract):
    @pytest.fixture
    def root(self, dav_server):
        pytest.importorskip("requests")
        from pathlib_next.uri.schemes.dav import DavPath
        return DavPath(dav_server)


# S3 contract
class TestS3Contract(PathContract):
    @pytest.fixture
    def root(self, s3_server):
        pytest.importorskip("boto3")
        url, _endpoint = s3_server
        from pathlib_next.uri.schemes.s3 import S3Path
        return S3Path(url)


# SFTP contract — in-process paramiko server (no extra deps beyond sftp extra)
class TestSftpContract(PathContract):
    @pytest.fixture
    def root(self, sftp_server):
        pytest.importorskip("paramiko")
        import paramiko
        from pathlib_next.uri.schemes.sftp import SftpPath, SftpBackend
        # Disable agent + key-file lookup so paramiko falls through to 'none'
        # auth, which is what our in-process test server accepts.
        backend = SftpBackend(
            {"allow_agent": False, "look_for_keys": False},
            paramiko.AutoAddPolicy(),
        )
        return SftpPath(sftp_server, backend=backend)

