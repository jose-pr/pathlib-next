"""Operations must reach the location they name.

rename()/move() used the SOURCE's connection, bucket or archive with only
`target.path`, so a target on another host, bucket, container, archive or
scheme was renamed in the wrong place and nothing raised. A backend taken
from a join segment also followed an absolute URI to another host, carrying
its session credentials or token there. Each test asserts where the data (or
the backend) actually ended up.
"""

import errno
import os
import zipfile

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath

pytest.importorskip("uritools")

from pathlib_next.uri import Source, UriPath  # noqa: E402

LocalPath = pathlib_next.LocalPath


# --- backend inheritance --------------------------------------------------


def test_http_session_backend_does_not_follow_join_to_another_host():
    requests = pytest.importorskip("requests")
    base = UriPath("http://trusted.invalid/api/").with_session(
        requests.Session(), auth=("svc", "s3cret")
    )
    assert (base / "child").backend is base.backend
    assert (base / "http://other.invalid/steal").backend is not base.backend
    assert UriPath(base, "http://other.invalid/steal").backend is not base.backend


def test_with_source_keeps_backend_only_for_the_same_authority():
    requests = pytest.importorskip("requests")
    base = UriPath("http://trusted.invalid/api/").with_session(
        requests.Session(), auth=("svc", "s3cret")
    )
    same = base.with_source(Source("http", None, "trusted.invalid", None))
    other = base.with_source(Source("http", None, "other.invalid", None))
    assert same.backend is base.backend
    assert other.backend is not base.backend


def test_github_token_backend_does_not_follow_join_to_another_host():
    pytest.importorskip("requests")
    from pathlib_next.uri.schemes.github import GitHubPath

    root = GitHubPath("github://ghp_SECRET@github.com/acme/widgets")
    evil = root / "github://evil.invalid/x/y"
    assert evil.backend is not root.backend
    assert evil.backend.token != "ghp_SECRET"
    assert (root / "README.md").backend is root.backend


# --- rename() refuses other locations ------------------------------------


def test_ftp_rename_to_another_server_raises_not_implemented():
    src = UriPath("ftp://a.invalid/report.csv")
    with pytest.raises(NotImplementedError):
        src.rename(UriPath("ftp://b.invalid/sub/report.csv"))


def test_sftp_rename_and_hardlink_to_another_host_raise_not_implemented():
    pytest.importorskip("paramiko")
    src = UriPath("sftp://a.invalid/data/report.csv")
    with pytest.raises(NotImplementedError):
        src.rename(UriPath("sftp://b.invalid/incoming/report.csv"))
    with pytest.raises(NotImplementedError):
        src.rename(LocalPath("report.csv").absolute())


def test_sftp_concurrent_copy_is_not_used_for_a_foreign_target(monkeypatch):
    pytest.importorskip("asyncssh")
    from pathlib_next.path import Path
    from pathlib_next.uri.schemes.sftp import SftpPath
    from pathlib_next.uri.schemes.sftp._asyncssh import AsyncsshSftpBackend

    calls = []
    monkeypatch.setattr(Path, "copy", lambda self, target, **kw: calls.append(target))
    # A directory source: the fan-out condition must fail on the target alone.
    monkeypatch.setattr(SftpPath, "is_dir", lambda self, **kw: True)
    src = SftpPath("sftp://a.invalid/data", backend=AsyncsshSftpBackend())
    local = LocalPath("dl").absolute()
    SftpPath.copy(src, local, recursive=True)
    other_host = SftpPath("sftp://b.invalid/backup", backend=AsyncsshSftpBackend())
    SftpPath.copy(src, other_host, recursive=True)
    assert calls == [local, other_host]


def test_s3_move_to_another_bucket_lands_in_that_bucket():
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws

    with mock_aws():
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bkt")
        client.create_bucket(Bucket="other")
        client.put_object(Bucket="bkt", Key="in/report.csv", Body=b"NEW")
        client.put_object(Bucket="bkt", Key="archive/report.csv", Body=b"PRECIOUS")

        src = UriPath("s3://bkt/in/report.csv")
        with pytest.raises(NotImplementedError):
            src.rename(UriPath("s3://other/archive/report.csv"))
        src.move(UriPath("s3://other/archive/report.csv"))

        def keys(bucket):
            return {
                o["Key"]: client.get_object(Bucket=bucket, Key=o["Key"])["Body"].read()
                for o in client.list_objects_v2(Bucket=bucket).get("Contents", [])
            }

        assert keys("other") == {"archive/report.csv": b"NEW"}
        assert keys("bkt") == {"archive/report.csv": b"PRECIOUS"}


def test_s3_rename_onto_itself_keeps_the_object():
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws

    with mock_aws():
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bkt")
        client.put_object(Bucket="bkt", Key="self.txt", Body=b"KEEP")
        UriPath("s3://bkt/self.txt").rename("self.txt")
        assert client.get_object(Bucket="bkt", Key="self.txt")["Body"].read() == b"KEEP"


class _AzBlob:
    def __init__(self, container, name):
        self._container = container
        self.name = name
        self.url = f"https://acct.invalid/cont/{name}"

    def start_copy_from_url(self, url):
        source = url.rsplit("/cont/", 1)[1]
        self._container.objects[self.name] = self._container.objects[source]
        return {"copy_status": "pending"}

    def get_blob_properties(self):
        class _Copy:
            status = "success"

        class _Props:
            copy = _Copy()

            def __getitem__(self, key):
                raise KeyError(key)

        return _Props()

    def delete_blob(self):
        del self._container.objects[self.name]


class _AzContainer:
    def __init__(self):
        self.objects = {}

    def get_blob_client(self, name):
        return _AzBlob(self, name)


def _az_path(uri, container):
    from pathlib_next.uri.schemes.az import AzPath, BaseAzBackend

    class _Client:
        def get_container_client(self, _name):
            return container

    class _Backend(BaseAzBackend):
        def client(self):
            return _Client()

    return AzPath(uri, backend=_Backend())


def test_az_rename_with_str_target_and_pending_copy_poll():
    container = _AzContainer()
    container.objects["a.txt"] = b"A"
    _az_path("az://acct/cont/a.txt", container).rename("b.txt")
    assert container.objects == {"b.txt": b"A"}


def test_az_rename_onto_itself_or_other_container():
    container = _AzContainer()
    container.objects["self.txt"] = b"KEEP"
    path = _az_path("az://acct/cont/self.txt", container)
    path.rename("self.txt")
    assert container.objects == {"self.txt": b"KEEP"}
    with pytest.raises(NotImplementedError):
        path.rename(UriPath("az://acct/othercont/self.txt"))


# --- archives -------------------------------------------------------------


def _zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_zip_member_move_to_local_path_extracts_it(tmp_path):
    archive = _zip(tmp_path / "a.zip", {"x.txt": "X", "keep.txt": "K"})
    member = UriPath(f"zip:{archive.as_uri()}!/x.txt")
    out = LocalPath(tmp_path / "out.txt")
    member.move(out)
    assert out.read_text() == "X"
    with zipfile.ZipFile(archive) as z:
        assert sorted(z.namelist()) == ["keep.txt"]


def test_zip_member_rename_into_another_archive_raises(tmp_path):
    a = _zip(tmp_path / "a.zip", {"x.txt": "X"})
    b = _zip(tmp_path / "b.zip", {"y.txt": "Y"})
    member = UriPath(f"zip:{a.as_uri()}!/x.txt")
    with pytest.raises(NotImplementedError):
        member.rename(UriPath(f"zip:{b.as_uri()}!/x.txt"))
    with zipfile.ZipFile(a) as z:
        assert z.namelist() == ["x.txt"]
    with zipfile.ZipFile(b) as z:
        assert z.namelist() == ["y.txt"]


# --- file: ----------------------------------------------------------------


def test_file_uri_rename_relative_str_is_a_sibling_rename(tmp_path, monkeypatch):
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.txt").write_text("A")
    result = UriPath(LocalPath(data / "a.txt")).rename("b.txt")
    assert (data / "b.txt").read_text() == "A"
    assert not (elsewhere / "b.txt").exists()
    assert type(result).__name__ == "FileUri"


def test_file_uri_rename_to_remote_target_raises(tmp_path):
    pytest.importorskip("paramiko")
    (tmp_path / "a.txt").write_text("A")
    src = UriPath(LocalPath(tmp_path / "a.txt"))
    with pytest.raises(NotImplementedError):
        src.rename(UriPath("sftp://host.invalid/srv/a.txt"))
    assert (tmp_path / "a.txt").read_text() == "A"


# --- Path.move() ----------------------------------------------------------


def test_local_move_onto_mempath_copies_instead_of_renaming(tmp_path):
    src = LocalPath(tmp_path / "upload.csv")
    src.write_text("DATA")
    target = MemPath("/") / "upload.csv"
    src.move(target)
    assert target.read_text() == "DATA"
    assert not src.exists()


def test_mempath_move_with_str_destination_stays_in_the_same_backend():
    root = MemPath("/")
    src = root / "src.txt"
    src.write_text("DATA")
    src.move("/c.txt")
    assert (root / "c.txt").read_text() == "DATA"
    assert not src.exists()


def test_mempath_copy_with_str_destination_stays_in_the_same_backend():
    root = MemPath("/")
    src = root / "src.txt"
    src.write_text("DATA")
    src.copy("/b.txt")
    assert (root / "b.txt").read_text() == "DATA"


def test_move_falls_back_to_copy_on_cross_device_rename(tmp_path, monkeypatch):
    def exdev(self, target):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(LocalPath, "rename", exdev)
    monkeypatch.setattr(LocalPath, "replace", exdev)
    src = LocalPath(tmp_path / "a.txt")
    src.write_text("A")
    target = LocalPath(tmp_path / "b.txt")
    target.write_text("OLD")
    src.move(target, overwrite=True)
    assert target.read_text() == "A"
    assert not src.exists()

    tree = LocalPath(tmp_path / "tree")
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.txt").write_text("F")
    dest = LocalPath(tmp_path / "moved")
    tree.move(dest)
    assert (dest / "sub" / "f.txt").read_text() == "F"
    assert not tree.exists()
