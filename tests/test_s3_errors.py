"""How `s3:` reports a failed request: the OSError type, nothing chained, and
the path named. Failures come from moto, from a client whose calls raise
botocore's own exceptions, and from loopback sockets (a closed port, a server
that cuts or stalls a body); nothing leaves the machine."""

import contextlib
import errno
import http.server
import socket
import threading
import time

import pytest

boto3 = pytest.importorskip("boto3")
botocore = pytest.importorskip("botocore")

import botocore.exceptions as _botoexc
from botocore.config import Config

from pathlib_next.mempath import MemPath
from pathlib_next.uri.schemes.s3 import BaseS3Backend, S3Backend, S3Path


def _reply(code, status, operation="Operation"):
    return _botoexc.ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class _Client:
    """A moto client with some methods replaced, and every call recorded."""

    def __init__(self, client, **overrides):
        self._client = client
        self._overrides = overrides
        self.calls = []

    def __getattr__(self, name):
        if name in self._overrides:
            target = self._overrides[name]
        else:
            target = getattr(self._client, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            self.calls.append(name)
            return target(*args, **kwargs)

        return call


class _Backend(BaseS3Backend):
    def __init__(self, client):
        self.proxy = client
        self.built = 0

    def client(self):
        self.built += 1
        return self.proxy


@pytest.fixture
def moto_s3(monkeypatch):
    pytest.importorskip("moto")
    from moto import mock_aws

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bkt")
        yield client


def _failing(error):
    def method(*args, **kwargs):
        raise error

    return method


def _keys(client):
    return sorted(
        obj["Key"] for obj in client.list_objects_v2(Bucket="bkt").get("Contents", [])
    )


def _path(moto, uri="s3://bkt/a.txt", **overrides):
    backend = _Backend(_Client(moto, **overrides))
    return S3Path(uri, backend=backend), backend.proxy


# --- the type a failure has ----------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        _botoexc.ReadTimeoutError(endpoint_url="http://example.invalid/bkt/a.txt"),
        _botoexc.ConnectTimeoutError(endpoint_url="http://example.invalid/bkt/a.txt"),
    ],
    ids=["read", "connect"],
)
def test_a_request_that_times_out_is_a_timeout_error_naming_the_path(moto_s3, error):
    path, _client = _path(moto_s3, head_object=_failing(error))
    with pytest.raises(TimeoutError) as info:
        path.stat()
    assert info.value.errno == errno.ETIMEDOUT
    assert info.value.filename == "s3://bkt/a.txt"
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__
    # The SDK's own text carries the request URL.
    assert "example.invalid" not in str(info.value)


def test_an_endpoint_that_cannot_be_reached_is_a_connection_error(moto_s3):
    error = _botoexc.EndpointConnectionError(
        endpoint_url="http://u:pw@example.invalid/"
    )

    class Paginator:
        def paginate(self, **kwargs):
            raise error

    path, _client = _path(
        moto_s3,
        head_object=_failing(error),
        get_paginator=lambda name: Paginator(),
    )
    with pytest.raises(ConnectionError) as info:
        path.stat()
    assert not isinstance(info.value, (ConnectionResetError, TimeoutError))
    assert info.value.filename == "s3://bkt/a.txt"
    assert info.value.__cause__ is None
    assert "pw" not in str(info.value)
    assert path.exists() is False
    errors = []
    assert list(path.parent.walk(on_error=errors.append)) == []
    assert [type(error) for error in errors] == [ConnectionError]


def test_a_transport_failure_never_reads_as_a_missing_file(moto_s3):
    path, _client = _path(
        moto_s3, head_object=_failing(_botoexc.ConnectionClosedError(endpoint_url="x"))
    )
    with pytest.raises(OSError) as info:
        path.stat()
    assert not isinstance(info.value, FileNotFoundError)
    assert path.exists() is False


def test_a_denied_upload_is_a_permission_error(moto_s3):
    def denied(*args, **kwargs):
        # boto3 raises its own error from inside the handler of the reply.
        try:
            raise _reply("AccessDenied", 403, "PutObject")
        except _botoexc.ClientError:
            from boto3.exceptions import S3UploadFailedError

            raise S3UploadFailedError("Failed to upload <fileobj> to bkt/a.txt")

    path, client = _path(moto_s3, upload_fileobj=denied)
    with pytest.raises(PermissionError) as info:
        path.write_bytes(b"x")
    assert info.value.__cause__ is None
    assert _keys(moto_s3) == []


# --- a closed port ---------------------------------------------------------------


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _loopback_backend(port, **config):
    return S3Backend(
        endpoint_url=f"http://127.0.0.1:{port}",
        region_name="us-east-1",
        aws_access_key_id="key",
        aws_secret_access_key="secret",
        config=Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 1, "mode": "standard"},
            # Windows reports a refused loopback connection only after about
            # two seconds; a shorter timeout would be the error instead.
            connect_timeout=10,
            **config,
        ),
    )


def test_nothing_listening_is_a_connection_error():
    path = S3Path("s3://bkt/a.txt", backend=_loopback_backend(_closed_port()))
    with pytest.raises(ConnectionError) as info:
        path.stat()
    assert not isinstance(info.value, TimeoutError)
    assert info.value.__cause__ is None


# --- a body cut short -------------------------------------------------------------

SIZE = 256 * 1024


class _BodyServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    mode = "cut"

    def handle_error(self, request, client_address):
        pass  # a client that closes early is the point of these tests


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _head(self):
        self.send_response(200)
        self.send_header("Content-Length", str(SIZE))
        self.send_header("Last-Modified", "Sun, 12 Jul 2026 00:00:00 GMT")
        self.send_header("ETag", '"x"')
        self.end_headers()

    def do_HEAD(self):
        self._head()

    def do_GET(self):
        self._head()
        try:
            self.wfile.write(b"A" * (SIZE // 4))
            self.wfile.flush()
            if self.server.mode == "stall":
                time.sleep(3)
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.close_connection = True

    def log_message(self, *args):
        pass


@pytest.fixture
def body_server():
    server = _BodyServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_body_cut_short_is_an_oserror_naming_the_path(body_server):
    body_server.mode = "cut"
    path = S3Path(
        "s3://bkt/obj.bin",
        backend=_loopback_backend(
            body_server.server_address[1], response_checksum_validation="when_required"
        ),
    )
    with pytest.raises(ConnectionResetError) as info:
        path.read_bytes()
    assert info.value.filename == "s3://bkt/obj.bin"
    assert info.value.__cause__ is None
    target = MemPath("/copied.bin")
    with pytest.raises(OSError, match="obj.bin"):
        path.copy(target)
    assert not target.exists()


def test_a_body_that_stalls_is_a_timeout_error_naming_the_path(body_server):
    body_server.mode = "stall"
    path = S3Path(
        "s3://bkt/obj.bin",
        backend=_loopback_backend(
            body_server.server_address[1],
            read_timeout=1,
            response_checksum_validation="when_required",
        ),
    )
    with pytest.raises(TimeoutError) as info:
        path.read_bytes()
    assert info.value.filename == "s3://bkt/obj.bin"
    assert info.value.__cause__ is None


def test_a_read_that_fails_in_the_body_reader_is_translated(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="a.txt", Body=b"abc")

    class Body:
        def read(self, size):
            raise _botoexc.ResponseStreamingError(error="broken")

        def close(self):
            pass

    path, _client = _path(moto_s3, get_object=lambda **kw: {"Body": Body()})
    with pytest.raises(OSError) as info:
        path.read_bytes()
    assert info.value.filename == "s3://bkt/a.txt"
    assert info.value.__cause__ is None


# --- a batch delete that refuses single keys --------------------------------------


def test_each_key_a_batch_delete_refuses_is_offered_with_its_own_path(moto_s3):
    for name in ("t/a", "t/b", "t/c", "t/d", "t/e"):
        moto_s3.put_object(Bucket="bkt", Key=name, Body=b"x")
    real = moto_s3.delete_objects

    def partial(Bucket, Delete):
        refused = {"t/b": "AccessDenied", "t/d": "InternalError"}
        kept = [o for o in Delete["Objects"] if o["Key"] not in refused]
        real(Bucket=Bucket, Delete={"Objects": kept})
        return {
            "Deleted": kept,
            "Errors": [
                {"Key": key, "Code": code, "Message": "refused"}
                for key, code in refused.items()
            ],
        }

    path, _client = _path(moto_s3, "s3://bkt/t", delete_objects=partial)
    offered = []
    path.rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append(
            (type(error), where.key, error.filename)
        )
        or True,
    )
    assert offered == [
        (PermissionError, "t/b", "s3://bkt/t/b"),
        (OSError, "t/d", "s3://bkt/t/d"),
    ]
    assert _keys(moto_s3) == ["t/b", "t/d"]


def test_a_refused_key_without_a_handler_is_raised_with_its_path(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="t/a", Body=b"x")

    def refuse(Bucket, Delete):
        return {
            "Errors": [{"Key": "t/a", "Code": "AccessDenied", "Message": "refused"}]
        }

    path, _client = _path(moto_s3, "s3://bkt/t", delete_objects=refuse)
    with pytest.raises(PermissionError) as info:
        path.rm(recursive=True)
    assert info.value.filename == "s3://bkt/t/a"
    assert "delete_objects" in str(info.value)


# --- paths that name no object -----------------------------------------------------


def test_a_bucket_root_is_a_directory_to_read_or_write(moto_s3):
    path, client = _path(moto_s3, "s3://bkt/")
    with pytest.raises(IsADirectoryError):
        path.read_bytes()
    with pytest.raises(IsADirectoryError):
        path.write_bytes(b"x")
    with pytest.raises(IsADirectoryError):
        path.open("r+b")
    assert client.calls == []
    assert _keys(moto_s3) == []


@pytest.mark.parametrize("uri", ["s3:x", "s3:///x", "s3:///"])
def test_a_path_without_a_bucket_does_not_exist_and_asks_nothing(moto_s3, uri):
    path, client = _path(moto_s3, uri)
    assert path.exists() is False
    assert path.is_dir() is False
    with pytest.raises(FileNotFoundError):
        path.stat()
    with pytest.raises(FileNotFoundError):
        path.read_bytes()
    with pytest.raises(FileNotFoundError):
        path.write_bytes(b"x")
    with pytest.raises(FileNotFoundError):
        list(path.iterdir())
    assert client.calls == []


def test_removing_a_missing_bucket_with_missing_ok_is_not_an_error(moto_s3):
    path = S3Path("s3://nobucket/x", backend=_Backend(_Client(moto_s3)))
    assert path.rm(recursive=True, missing_ok=True) is None
    with pytest.raises(FileNotFoundError):
        path.rm(recursive=True)
    offered = []
    path.rm(
        recursive=True,
        ignore_error=lambda error, where: offered.append(type(error)) or True,
    )
    assert offered == [FileNotFoundError]


def test_renaming_a_missing_path_onto_its_own_name_is_file_not_found(moto_s3):
    moto_s3.put_object(Bucket="bkt", Key="there.txt", Body=b"x")
    moto_s3.put_object(Bucket="bkt", Key="dir/f", Body=b"x")
    backend = _Backend(_Client(moto_s3))
    with pytest.raises(FileNotFoundError):
        S3Path("s3://bkt/nope", backend=backend).rename("nope")
    assert _keys(moto_s3) == ["dir/f", "there.txt"]
    # A name that is there is a rename that changes nothing.
    there = S3Path("s3://bkt/there.txt", backend=backend)
    assert there.rename("there.txt") == there
    assert S3Path("s3://bkt/dir", backend=backend).rename("dir").key == "dir"
    assert _keys(moto_s3) == ["dir/f", "there.txt"]


# --- what a move of a prefix asks the store ------------------------------------------


def test_renaming_a_prefix_directory_asks_for_the_key_once_and_then_for_its_keys(
    moto_s3,
):
    moto_s3.put_object(Bucket="bkt", Key="dir/f", Body=b"x")
    path, client = _path(moto_s3, "s3://bkt/dir")
    with pytest.raises(NotImplementedError):
        path.rename("other")
    assert client.calls == ["head_object", "list_objects_v2"]
    assert _keys(moto_s3) == ["dir/f"]


def test_renaming_a_missing_path_is_file_not_found_after_one_head_and_one_listing(
    moto_s3,
):
    path, client = _path(moto_s3, "s3://bkt/nope")
    with pytest.raises(FileNotFoundError):
        path.rename("other")
    assert client.calls == ["head_object", "list_objects_v2"]
