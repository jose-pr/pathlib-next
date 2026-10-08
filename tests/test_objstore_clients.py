"""The clients of the object-store backends: one per backend however many
threads ask first, and none inside a pickled or copied backend or path."""

import copy
import pickle
import sys
import threading
import time
import types

import pytest

from pathlib_next.uri.schemes.az import AzBackend, AzPath
from pathlib_next.uri.schemes.gs import GsBackend, GsPath
from pathlib_next.uri.schemes.s3 import S3Backend, S3Path

THREADS = 24


def _asked_at_once(backend):
    """What `THREADS` threads get from `backend.client()` when they all ask
    at the same moment."""
    gate = threading.Barrier(THREADS)
    got = []

    def ask():
        gate.wait()
        got.append(backend.client())

    threads = [threading.Thread(target=ask) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(got) == THREADS
    return got


class _SlowBuilds:
    """Records what is built, slowly enough for a race to show."""

    def __init__(self):
        self.built = []

    def __call__(self, *args, **kwargs):
        time.sleep(0.02)
        client = object()
        self.built.append(client)
        return client


def _install(monkeypatch, name, **attributes):
    """A stand-in for the module `name` with `attributes`, parents included."""
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    parts = name.split(".")
    for depth in range(1, len(parts)):
        parent = ".".join(parts[:depth])
        if parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_threads_racing_for_a_fresh_s3_backend_build_one_client(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    builds = _SlowBuilds()
    monkeypatch.setattr(boto3, "client", builds)
    got = _asked_at_once(S3Backend())
    assert len(builds.built) == 1
    assert all(client is builds.built[0] for client in got)


def test_threads_racing_for_a_fresh_gs_backend_build_one_client(monkeypatch):
    builds = _SlowBuilds()
    _install(monkeypatch, "google.cloud.storage", Client=builds)
    got = _asked_at_once(GsBackend(project="p"))
    assert len(builds.built) == 1
    assert all(client is builds.built[0] for client in got)


def test_threads_racing_for_a_fresh_az_backend_build_one_client_and_credential(
    monkeypatch,
):
    from pathlib_next.uri.schemes import az

    credentials = _SlowBuilds()
    monkeypatch.setattr(az, "_default_credential", credentials)
    builds = _SlowBuilds()
    _install(monkeypatch, "azure.storage.blob", BlobServiceClient=builds)
    got = _asked_at_once(AzBackend(account="acct"))
    assert len(builds.built) == 1 and len(credentials.built) == 1
    assert all(client is builds.built[0] for client in got)


# --- a backend pickles and copies without its client -------------------------------


def _roundtrips(backend):
    yield pickle.loads(pickle.dumps(backend))
    yield copy.copy(backend)
    yield copy.deepcopy(backend)


def _check_clientless_copies(backend, attributes):
    first = backend.client()
    assert backend.client() is first
    for other in _roundtrips(backend):
        assert type(other) is type(backend)
        assert other._client is None
        assert {name: getattr(other, name) for name in attributes} == {
            name: getattr(backend, name) for name in attributes
        }
        built = other.client()
        assert built is not first and other.client() is built
    # The backend itself keeps the client it built.
    assert backend.client() is first


def test_an_s3_backend_pickles_and_copies_after_its_client_was_built():
    pytest.importorskip("boto3")
    backend = S3Backend(
        region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s"
    )
    _check_clientless_copies(backend, ["client_kwargs"])


def test_a_gs_backend_pickles_and_copies_after_its_client_was_built():
    pytest.importorskip("google.cloud.storage")
    backend = GsBackend(
        project="p",
        client_options={"api_endpoint": "http://127.0.0.1:9"},
        use_auth_w_custom_endpoint=False,
        timeout=3,
    )
    _check_clientless_copies(backend, ["client_kwargs", "_options"])
    assert pickle.loads(pickle.dumps(backend)).call_options() == {"timeout": 3}


def test_an_az_backend_pickles_and_copies_after_its_client_was_built():
    pytest.importorskip("azure.storage.blob")
    backend = AzBackend(
        account_url="https://acct.blob.core.windows.net/?sv=2024-01-01&sig=x",
        retry_total=1,
    )
    _check_clientless_copies(backend, ["client_kwargs", "account"])


class _Extended(S3Backend):
    """A subclass keeping state of its own, in a `__dict__`."""

    def __init__(self, label, **client_kwargs):
        super().__init__(**client_kwargs)
        self.label = label


def test_a_subclass_keeps_its_own_state_when_pickled_or_copied(monkeypatch):
    pytest.importorskip("boto3")
    backend = _Extended("mine", region_name="us-east-1")
    for other in _roundtrips(backend):
        assert isinstance(other, _Extended) and other.label == "mine"
        assert other.client_kwargs == {"region_name": "us-east-1"}


# --- a path pickles without any of it ------------------------------------------------

SECRETS = (b"AKIAEXAMPLEACCESSKEY", b"EXAMPLESECRETVALUE", b"EXAMPLESESSIONTOKEN")


def _secret_backend(scheme):
    if scheme == "s3":
        return S3Backend(
            region_name="us-east-1",
            aws_access_key_id=SECRETS[0].decode(),
            aws_secret_access_key=SECRETS[1].decode(),
            aws_session_token=SECRETS[2].decode(),
        )
    if scheme == "gs":
        return GsBackend(
            project=SECRETS[0].decode(),
            client_options={"quota_project_id": SECRETS[1].decode()},
            extra_headers={"x-token": SECRETS[2].decode()},
        )
    return AzBackend(
        account_url=f"https://acct.blob.core.windows.net/?sig={SECRETS[1].decode()}",
        credential=SECRETS[2].decode(),
        connection_verify=SECRETS[0].decode(),
    )


@pytest.mark.parametrize(
    "scheme, uri, cls",
    [
        ("s3", "s3://bkt/a.txt", S3Path),
        ("gs", "gs://bkt/a.txt", GsPath),
        ("az", "az://acct/cont/a.txt", AzPath),
    ],
)
def test_a_pickled_path_carries_no_client_kwargs_and_no_client(scheme, uri, cls):
    backend = _secret_backend(scheme)
    assert not getattr(backend, "picklable", False)
    path = cls(uri, backend=backend)
    data = pickle.dumps(path)
    for secret in SECRETS:
        assert secret not in data
    assert b"botocore" not in data and b"boto3" not in data
    assert b"google.cloud" not in data and b"azure.storage" not in data
    again = pickle.loads(data)
    assert type(again) is cls and str(again) == uri
    # The receiving process builds the default backend, not this one.
    assert again.backend is not backend
    assert getattr(again.backend, "client_kwargs", {}) == {}


def test_an_s3_path_pickles_and_deep_copies_after_it_did_io(monkeypatch):
    pytest.importorskip("boto3")
    pytest.importorskip("moto")
    import boto3
    from moto import mock_aws

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="bkt")
        backend = S3Backend(
            region_name="us-east-1",
            aws_access_key_id=SECRETS[0].decode(),
            aws_secret_access_key=SECRETS[1].decode(),
            aws_session_token=SECRETS[2].decode(),
        )
        path = S3Path("s3://bkt/dir/a.txt", backend=backend)
        path.write_bytes(b"x")
        assert path.exists()
        children = list(path.parent.iterdir())
        for item in [path, path.parent, *children]:
            data = pickle.dumps(item)
            assert not any(secret in data for secret in SECRETS)
            assert str(pickle.loads(data)) == str(item)
            assert str(copy.deepcopy(item)) == str(item)
        # A path built for itself pickles after its first request, too.
        derived = S3Path("s3://bkt/dir/a.txt")
        assert derived.read_bytes() == b"x"
        assert str(pickle.loads(pickle.dumps(derived))) == str(derived)
        assert copy.deepcopy(derived).read_bytes() == b"x"
