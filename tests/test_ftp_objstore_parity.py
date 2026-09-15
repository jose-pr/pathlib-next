"""pathlib parity and soundness of the ftp: and s3: schemes, end to end:
loopback pyftpdlib servers (built here, with per-test handler settings) and
moto for S3. No external hosts."""

import errno
import ftplib
import gc
import io
import os
import stat
import threading
import time
import warnings

import pytest

from pathlib_next import LocalPath
from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes import ftp as ftp_mod
from pathlib_next.uri.schemes.ftp import FtpBackend, FtpPath

# --- FTP loopback servers ------------------------------------------------------


@pytest.fixture
def ftp_factory(tmp_path):
    """`start(perm=..., mlsd=..., timeout=...)` -> (url, handler class, root).
    The handler counts the control connections it accepted."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.ioloop import Select
        from pyftpdlib.servers import FTPServer

    started = []

    def start(perm="elradfmwMT", mlsd=True, timeout=None):
        root = tmp_path / f"root{len(started)}"
        (root / "d" / "sub").mkdir(parents=True)
        (root / "d" / "f.txt").write_bytes(b"hello")
        authorizer = DummyAuthorizer()
        authorizer.add_user("user", "12345", str(root), perm=perm)

        class Handler(FTPHandler):
            connections = 0

            def on_connect(self):
                type(self).connections += 1

        Handler.authorizer = authorizer
        if not mlsd:
            Handler.proto_cmds = {
                name: spec
                for name, spec in FTPHandler.proto_cmds.items()
                if name not in ("MLSD", "MLST")
            }
        if timeout is not None:
            Handler.timeout = timeout
        ioloop = Select()
        server = FTPServer(("127.0.0.1", 0), Handler, ioloop=ioloop)

        def serve():
            try:
                server.serve_forever()
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        port = server.socket.getsockname()[1]
        started.append((server, ioloop, thread, port))
        return f"ftp://user:12345@127.0.0.1:{port}/", Handler, root

    yield start
    for server, ioloop, thread, port in started:
        # Close this test's cached connections before their server goes.
        for key in list(ftp_mod._CACHED_CLIENTS.cache):
            if key[1].port == port:
                ftp_mod._CACHED_CLIENTS.discard(*key)
        ioloop.call_later(0, server.close_all)
        thread.join(timeout=5)


def test_ftp_separately_built_paths_share_one_connection(ftp_factory):
    url, handler, root = ftp_factory()
    for i in range(10):
        (root / f"f{i}.txt").write_bytes(b"%d" % i)
    for i in range(10):
        assert UriPath(f"{url}f{i}.txt").read_bytes() == b"%d" % i
    assert handler.connections == 1


def test_ftp_evicted_connection_is_closed(ftp_factory, monkeypatch):
    url, _handler, _root = ftp_factory()
    p = FtpPath(f"{url}d/f.txt")
    p.read_bytes()
    ((key, client),) = [
        (k, c) for k, c in ftp_mod._CACHED_CLIENTS.cache.items() if k[1] == p.source
    ]
    ftp_mod._CACHED_CLIENTS.discard(*key)
    assert client.sock is None  # ftplib.FTP.close() clears it


class _Interrupt(BaseException):
    """Stands in for a KeyboardInterrupt (or a decode error) raised in the
    middle of a transfer."""


def test_ftp_failed_transfer_does_not_desync_the_next_operation(
    ftp_factory, monkeypatch
):
    url, _handler, root = ftp_factory()
    (root / "small.bin").write_bytes(b"x" * 100)
    base_cls = ftplib.FTP

    class InterruptOnce(base_cls):
        armed = True

        def retrbinary(self, cmd, callback, *args, **kwargs):
            def interrupting(data):
                if type(self).armed:
                    type(self).armed = False
                    # Let the server finish and queue its 226 first.
                    time.sleep(0.2)
                    raise _Interrupt()
                callback(data)

            return super().retrbinary(cmd, interrupting, *args, **kwargs)

    monkeypatch.setattr(ftplib, "FTP", InterruptOnce)
    backend = FtpBackend(timeout=5)
    p = FtpPath(f"{url}small.bin", backend=backend)
    with pytest.raises(_Interrupt):
        p.read_bytes()
    assert p.read_bytes() == b"x" * 100
    assert p.stat().st_size == 100
    FtpPath(f"{url}c.txt", backend=backend).write_bytes(b"cc")
    assert (root / "c.txt").read_bytes() == b"cc"


def test_ftp_write_after_idle_timeout_reconnects_and_lands(ftp_factory):
    url, handler, root = ftp_factory(timeout=1)
    p = FtpPath(f"{url}late.txt", backend=FtpBackend(timeout=5))
    assert p.parent.is_dir()  # the session exists before the idle wait
    f = p.open("wb")
    f.write(b"important data")
    time.sleep(2.5)  # the server drops the idle session
    f.close()
    assert (root / "late.txt").read_bytes() == b"important data"
    assert handler.connections == 2


def test_ftp_without_mlsd_directories_stat_as_directories(ftp_factory):
    url, _handler, _root = ftp_factory(mlsd=False)
    base = FtpPath(url)
    d = base / "d"
    assert d.exists()
    assert d.is_dir()
    f = d / "f.txt"
    assert f.is_file()
    assert f.stat().st_size == 5
    assert not (base / "nope").exists()
    d.mkdir(parents=True, exist_ok=True)
    walked = [(p.name, sorted(ds), sorted(fs)) for p, ds, fs in d.walk()]
    assert walked == [("d", ["sub"], ["f.txt"]), ("sub", [], [])]
    with pytest.raises(NotADirectoryError):
        list(f.iterdir())
    with pytest.raises(FileNotFoundError):
        list((base / "nope").iterdir())


def test_ftp_refused_login_is_permission_error_not_missing(ftp_factory):
    url, handler, _root = ftp_factory()
    handler.auth_failed_timeout = 0.01  # pyftpdlib delays a failed login 3 s
    p = FtpPath(url.replace(":12345@", ":wrong@") + "d/f.txt")
    with pytest.raises(PermissionError):
        p.stat()
    with pytest.raises(PermissionError):
        p.read_bytes()


def test_ftp_mlsd_mtime_is_utc(ftp_factory):
    url, _handler, root = ftp_factory()
    epoch = 1577880000  # 2020-01-01T12:00:00Z
    target = root / "t.txt"
    target.write_bytes(b"t")
    os.utime(target, (epoch, epoch))
    assert FtpPath(f"{url}t.txt").stat().st_mtime == epoch


def test_ftp_refused_commands_map_to_permission_error(ftp_factory):
    url, _handler, root = ftp_factory(perm="elr")
    keep = FtpPath(f"{url}d/f.txt")
    with pytest.raises(PermissionError):
        keep.unlink()
    with pytest.raises(PermissionError):
        keep.unlink(missing_ok=True)
    assert (root / "d" / "f.txt").exists()
    # A read-only login gets "550 Not enough privileges" for a missing file
    # too; it is still missing.
    missing = FtpPath(f"{url}missing.txt")
    missing.unlink(missing_ok=True)
    with pytest.raises(FileNotFoundError):
        missing.unlink()
    with pytest.raises(PermissionError):
        FtpPath(f"{url}new.txt").write_bytes(b"x")
    with pytest.raises(PermissionError):
        FtpPath(f"{url}d").rmdir()
    with pytest.raises(PermissionError):
        FtpPath(f"{url}d/f.txt").rename("g.txt")


def test_ftp_wrong_type_and_missing_targets_raise_like_pathlib(ftp_factory):
    url, _handler, root = ftp_factory()
    d = FtpPath(f"{url}d")
    with pytest.raises(IsADirectoryError):
        d.read_bytes()
    with pytest.raises(IsADirectoryError):
        d.unlink()
    with pytest.raises(NotADirectoryError):
        FtpPath(f"{url}d/f.txt").rmdir()
    with pytest.raises(OSError) as info:
        d.rmdir()
    assert info.value.errno == errno.ENOTEMPTY
    nope = FtpPath(f"{url}nope")
    with pytest.raises(FileNotFoundError):
        nope.rmdir()
    with pytest.raises(FileNotFoundError):
        nope.rename("other")
    with pytest.raises(FileNotFoundError):
        nope.chmod(0o644)
    with pytest.raises(FileNotFoundError):
        list(nope.iterdir())
    with pytest.raises(NotADirectoryError):
        list(FtpPath(f"{url}d/f.txt").iterdir())
    errors = []
    assert list(nope.walk(on_error=errors.append)) == []
    assert [type(e) for e in errors] == [FileNotFoundError]
    with pytest.raises(FileNotFoundError):
        FtpPath(f"{url}nope/x.txt").write_bytes(b"x")
    FtpPath(f"{url}a/b/c").mkdir(parents=True)
    assert (root / "a" / "b" / "c").is_dir()


def test_ftp_rplus_writes_land_and_read_stream_is_read_only(ftp_factory):
    url, _handler, root = ftp_factory()
    p = FtpPath(f"{url}d/f.txt")
    with p.open("rb") as f:
        with pytest.raises(io.UnsupportedOperation):
            f.write(b"x")
    with p.open("r+b") as f:
        assert f.read() == b"hello"
        f.seek(0)
        f.write(b"J")
    assert (root / "d" / "f.txt").read_bytes() == b"Jello"


def test_ftp_failed_upload_closes_the_stream(ftp_factory):
    url, _handler, _root = ftp_factory(perm="elr")
    f = FtpPath(f"{url}state.json").open("wb")
    f.write(b"v1")
    with pytest.raises(PermissionError):
        f.close()
    assert f.closed


def test_ftp_mlsd_perm_fact_is_a_known_mode(ftp_factory, tmp_path):
    url, _handler, _root = ftp_factory()
    st = FtpPath(f"{url}d/f.txt").stat()
    assert st.mode_known
    assert stat.S_ISREG(st.st_mode)
    assert stat.S_IMODE(st.st_mode) & 0o600 == 0o600
    assert FtpPath(f"{url}d").stat().mode_known
    out = LocalPath(tmp_path / "out.txt")
    FtpPath(f"{url}d/f.txt").copy(out)
    assert os.access(out, os.W_OK)
    FtpPath(f"{url}d/f.txt").copy(out, overwrite=True)
    assert out.read_bytes() == b"hello"


def test_ftp_mlsd_unix_mode_fact_is_used():
    st = FtpPath("ftp://host/x")._facts_to_filestat(
        {"type": "file", "size": "1", "unix.mode": "0755", "perm": "r"}
    )
    assert st.mode_known
    assert st.st_mode == stat.S_IFREG | 0o755
    st = FtpPath("ftp://host/x")._facts_to_filestat({"type": "dir"})
    assert not st.mode_known


# --- S3 (moto) -------------------------------------------------------------------

botocore = pytest.importorskip("botocore")

from botocore.exceptions import ClientError  # noqa: E402

from pathlib_next.uri.schemes import s3 as s3_mod  # noqa: E402
from pathlib_next.uri.schemes.s3 import BaseS3Backend, S3Path  # noqa: E402


@pytest.fixture
def moto_s3(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bkt")
        yield client


def _error(code, status, operation="Operation"):
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class _Proxy:
    """The moto client with some methods replaced."""

    def __init__(self, client, **overrides):
        self._client = client
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._client, name)


class _ProxyBackend(BaseS3Backend):
    def __init__(self, proxy):
        self.proxy = proxy

    def client(self):
        return self.proxy


def _keys(client):
    return sorted(
        obj["Key"] for obj in client.list_objects_v2(Bucket="bkt").get("Contents", [])
    )


def _raise(error):
    def method(*args, **kwargs):
        raise error

    return method


def test_s3_403_is_permission_error_and_never_escapes_exists(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.txt", Body=b"a")
    denied = _raise(_error("403", 403, "HeadObject"))
    p = S3Path(
        "s3://bkt/a.txt", backend=_ProxyBackend(_Proxy(moto_s3, head_object=denied))
    )
    with pytest.raises(PermissionError):
        p.stat()
    assert p.exists() is False
    assert p.is_file() is False
    assert p.is_dir() is False


def test_s3_throttled_read_is_not_file_not_found(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.txt", Body=b"a")
    slow = _raise(_error("SlowDown", 503, "GetObject"))
    p = S3Path(
        "s3://bkt/a.txt", backend=_ProxyBackend(_Proxy(moto_s3, get_object=slow))
    )
    with pytest.raises(OSError) as info:
        p.read_bytes()
    assert type(info.value) is OSError
    assert isinstance(info.value.__cause__, ClientError)


def test_s3_missing_bucket_is_file_not_found(moto_s3):
    assert S3Path("s3://nobkt/k.txt").exists() is False
    errors = []
    assert list(S3Path("s3://nobkt/").walk(on_error=errors.append)) == []
    assert [type(e) for e in errors] == [FileNotFoundError]


def test_s3_missing_or_wrong_type_targets_raise_like_pathlib(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="file.txt", Body=b"x")
    moto_s3.put_object(Bucket="bkt", Key="plain/x.txt", Body=b"x")
    moto_s3.put_object(Bucket="bkt", Key="empty/", Body=b"")
    with pytest.raises(FileNotFoundError):
        list(S3Path("s3://bkt/typo").iterdir())
    with pytest.raises(NotADirectoryError):
        list(S3Path("s3://bkt/file.txt").iterdir())
    assert list(S3Path("s3://bkt/empty").iterdir()) == []
    with pytest.raises(FileNotFoundError):
        S3Path("s3://bkt/nope").rmdir()
    with pytest.raises(NotADirectoryError):
        S3Path("s3://bkt/file.txt").rmdir()
    with pytest.raises(OSError) as info:
        S3Path("s3://bkt/plain").rmdir()
    assert info.value.errno == errno.ENOTEMPTY
    with pytest.raises(IsADirectoryError):
        S3Path("s3://bkt/plain").unlink()
    assert S3Path("s3://bkt/plain").is_dir()
    with pytest.raises(IsADirectoryError):
        S3Path("s3://bkt/plain").read_bytes()
    assert _keys(moto_s3) == ["empty/", "file.txt", "plain/x.txt"]


def test_s3_failed_close_upload_is_not_retried_at_gc(moto_s3):
    failures = [_error("SlowDown", 503, "PutObject")]

    def upload_fileobj(fileobj, bucket, key, *args, **kwargs):
        if failures:
            raise failures.pop()
        return moto_s3.upload_fileobj(fileobj, bucket, key, *args, **kwargs)

    backend = _ProxyBackend(_Proxy(moto_s3, upload_fileobj=upload_fileobj))
    p = S3Path("s3://bkt/state.json", backend=backend)
    f = p.open("wb")
    f.write(b'{"version": 1}')
    with pytest.raises(OSError):
        f.close()
    assert f.closed
    p.write_bytes(b'{"version": 2}')
    del f
    gc.collect()
    assert p.read_bytes() == b'{"version": 2}'


def test_s3_prefix_directory_move_falls_back_to_copy_and_delete(moto_s3):
    for key in ("plain/a.txt", "plain/sub/b.txt", "keep.txt"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=key.encode())
    with pytest.raises(NotImplementedError):
        S3Path("s3://bkt/plain").rename("plain2")
    with pytest.raises(FileNotFoundError):
        S3Path("s3://bkt/nope").rename("nope2")
    S3Path("s3://bkt/plain").move("s3://bkt/plain3")
    assert _keys(moto_s3) == [
        "keep.txt",
        "plain3/",
        "plain3/a.txt",
        "plain3/sub/",
        "plain3/sub/b.txt",
    ]
    assert S3Path("s3://bkt/plain3/sub/b.txt").read_bytes() == b"plain/sub/b.txt"


def test_s3_large_write_is_multipart(moto_s3):
    import boto3
    from botocore.config import Config

    data = os.urandom(9 * 1024 * 1024)  # over boto3's 8 MiB threshold
    p = S3Path("s3://bkt/big.bin")
    with p.open("wb") as f:
        for offset in range(0, len(data), 1024 * 1024):
            f.write(data[offset : offset + 1024 * 1024])
    head = moto_s3.head_object(Bucket="bkt", Key="big.bin")
    assert head["ContentLength"] == len(data)
    assert head["ETag"].strip('"').endswith("-2")  # two parts
    # moto 5.1 (the 3.9 venv) reports a multipart object's composite CRC32
    # without its "-2" suffix, so botocore's response validation rejects
    # any read of it -- plain boto3 included. Real S3 and moto 5.2 mark it
    # COMPOSITE. Compare the bytes without that validation.
    reader = boto3.client(
        "s3",
        region_name="us-east-1",
        config=Config(response_checksum_validation="when_required"),
    )
    assert reader.get_object(Bucket="bkt", Key="big.bin")["Body"].read() == data


def test_s3_read_streams_the_body(moto_s3):
    data = os.urandom(1024 * 1024)
    moto_s3.put_object(Bucket="bkt", Key="big.bin", Body=data)
    sizes = []

    def get_object(**kwargs):
        response = moto_s3.get_object(**kwargs)
        body = response["Body"]

        class Recording:
            def read(self, amt=None):
                sizes.append(amt)
                return body.read(amt)

            def close(self):
                body.close()

        response["Body"] = Recording()
        return response

    streamed = S3Path(
        "s3://bkt/big.bin",
        backend=_ProxyBackend(_Proxy(moto_s3, get_object=get_object)),
    )
    with streamed.open("rb") as f:
        assert f.read(16) == data[:16]
        with pytest.raises(io.UnsupportedOperation):
            f.write(b"x")
    assert sizes and None not in sizes  # never one read of the whole body
    assert streamed.read_bytes() == data


def test_s3_rename_preserves_storage_class_and_uses_managed_copy_when_large(
    moto_s3, monkeypatch
):
    moto_s3.put_object(
        Bucket="bkt", Key="ia.txt", Body=b"ia", StorageClass="STANDARD_IA"
    )
    S3Path("s3://bkt/ia.txt").rename("ia2.txt")
    assert (
        moto_s3.head_object(Bucket="bkt", Key="ia2.txt")["StorageClass"]
        == "STANDARD_IA"
    )

    copies = []

    def copy(source, bucket, key, ExtraArgs=None, **kwargs):
        copies.append((source["Key"], key, ExtraArgs))
        return moto_s3.copy(source, bucket, key, ExtraArgs=ExtraArgs, **kwargs)

    monkeypatch.setattr(s3_mod, "_PUT_OBJECT_LIMIT", 1)
    backend = _ProxyBackend(_Proxy(moto_s3, copy=copy))
    S3Path("s3://bkt/ia2.txt", backend=backend).rename("ia3.txt")
    assert copies == [("ia2.txt", "ia3.txt", {"StorageClass": "STANDARD_IA"})]
    assert _keys(moto_s3) == ["ia3.txt"]
    assert moto_s3.get_object(Bucket="bkt", Key="ia3.txt")["Body"].read() == b"ia"


def test_s3_exclusive_create_is_atomic(moto_s3):
    first = S3Path("s3://bkt/locks/job.lock").open("xb")
    second = S3Path("s3://bkt/locks/job.lock").open("xb")
    first.write(b"worker1")
    second.write(b"worker2")
    first.close()
    with pytest.raises(FileExistsError):
        second.close()
    assert S3Path("s3://bkt/locks/job.lock").read_bytes() == b"worker1"
    with pytest.raises(FileExistsError):
        S3Path("s3://bkt/locks/job.lock").touch(exist_ok=False)


def test_s3_exclusive_create_without_conditional_writes_falls_back(moto_s3):
    def put_object(**kwargs):
        if "IfNoneMatch" in kwargs:
            raise _error("NotImplemented", 501, "PutObject")
        return moto_s3.put_object(**kwargs)

    backend = _ProxyBackend(_Proxy(moto_s3, put_object=put_object))
    p = S3Path("s3://bkt/new.txt", backend=backend)
    with p.open("xb") as f:
        f.write(b"created")
    assert p.read_bytes() == b"created"
    S3Path("s3://bkt/newdir", backend=backend).mkdir()
    assert "newdir/" in _keys(moto_s3)


def test_s3_rplus_writes_land(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.txt", Body=b"hello")
    p = S3Path("s3://bkt/a.txt")
    with p.open("r+b") as f:
        assert f.read() == b"hello"
        f.seek(0)
        f.write(b"J")
    assert p.read_bytes() == b"Jello"
    with p.open("r+") as f:
        f.seek(0, io.SEEK_END)
        f.write("!")
    assert p.read_text() == "Jello!"
