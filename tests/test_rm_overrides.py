"""Every scheme that overrides `rm()` keeps the signature of `Path.rm()`.

The overrides take their own route for a recursive removal (one DAV DELETE, a
batched object delete, a concurrent SFTP walk). `follow_symlinks=` and
`follow_binds=` are keywords of `Path.rm()`, so each override accepts them,
checks them before anything is sent, and forwards them wherever the generic
removal runs. Nothing here needs a cloud SDK: the signatures and the
argument checks are read without a call to a service.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("uritools")
pytest.importorskip("botocore")

from pathlib_next import Path
from pathlib_next.uri.schemes import az, dav, gs, s3, sftp

CASES = [
    (dav.DavPath, "dav://host/dir"),
    (sftp.SftpPath, "sftp://host/dir"),
    (s3.S3Path, "s3://bucket/dir"),
    (gs.GsPath, "gs://bucket/dir"),
    (az.AzPath, "az://account/container/dir"),
]
IDS = [cls.__name__ for cls, _ in CASES]


@pytest.mark.parametrize("cls, url", CASES, ids=IDS)
def test_rm_override_has_the_signature_of_path_rm(cls, url):
    expected = inspect.signature(Path.rm).parameters
    actual = inspect.signature(cls.rm).parameters
    assert list(actual) == list(expected)
    for name, parameter in expected.items():
        assert actual[name].kind == parameter.kind, name
        assert actual[name].default == parameter.default, name


@pytest.mark.parametrize("cls, url", CASES, ids=IDS)
def test_rm_non_recursive_forwards_the_policies(monkeypatch, cls, url):
    seen = {}

    def record(self, *args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(Path, "rm", record)
    policy = lambda path: None
    cls(url).rm(follow_symlinks=policy, follow_binds=None)
    assert seen["follow_symlinks"] is policy
    assert seen["follow_binds"] is None


@pytest.mark.parametrize("keyword", ["follow_symlinks", "follow_binds"])
@pytest.mark.parametrize("recursive", [False, True])
@pytest.mark.parametrize("cls, url", CASES, ids=IDS)
def test_rm_checks_the_policies_before_anything_is_sent(cls, url, recursive, keyword):
    """No client exists in these paths, so a request would not fail with
    `ValueError`."""
    with pytest.raises(ValueError, match="True, False, None or a callable"):
        cls(url).rm(recursive=recursive, ignore_error=True, **{keyword: "always"})


@pytest.fixture
def moto_s3(aws_test_credentials):
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")

    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="bkt")
        yield client


def test_rm_recursive_on_a_store_ignores_the_policies_it_has_no_links_for(moto_s3):
    """The same objects go with and without the keywords."""
    for key in ("tree/a.txt", "tree/sub/b.txt", "keep.txt"):
        moto_s3.put_object(Bucket="bkt", Key=key, Body=b"x")

    s3.S3Path("s3://bkt/tree").rm(
        recursive=True, follow_symlinks=True, follow_binds=lambda entry: None
    )

    listing = moto_s3.list_objects_v2(Bucket="bkt").get("Contents", [])
    assert [obj["Key"] for obj in listing] == ["keep.txt"]
