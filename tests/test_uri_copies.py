"""Copying and pickling a `UriPath`: a path is its URI. A pickle holds that and
nothing else by default, `copy.copy`/`copy.deepcopy` share the backend object
instead of duplicating a connection, and neither changes what `==`, `hash`,
`_same_filesystem()` and `_node_key()` answer."""

from __future__ import annotations

import copy
import dataclasses
import pickle
import zipfile

import pytest

pytest.importorskip("uritools")

from pathlib_next.uri import Uri, UriPath
from pathlib_next.uri.source import Source

SECRET = b"S3CR3T-dummy"


class _Backend:
    """A caller-supplied backend that counts how it is copied."""

    def __init__(self, secret="S3CR3T-dummy"):
        self.secret = secret
        self.copies = 0

    def __deepcopy__(self, memo):
        self.copies += 1
        return _Backend(self.secret)

    def __reduce__(self):
        raise AssertionError("a connection must not be pickled")


class _PicklableBackend:
    picklable = True

    def __init__(self, label="shared"):
        self.label = label

    def __eq__(self, other):
        return isinstance(other, _PicklableBackend) and other.label == self.label

    def __hash__(self):
        return hash(self.label)


def _roundtrip(path, protocol=pickle.HIGHEST_PROTOCOL):
    return pickle.loads(pickle.dumps(path, protocol))


# --- what a pickle holds ---------------------------------------------------------


@pytest.mark.parametrize("protocol", range(2, pickle.HIGHEST_PROTOCOL + 1))
@pytest.mark.parametrize(
    "url",
    [
        "http://h/a/b",
        "https://h:8443/a%20b/c?x=1&y=%2B#frag",
        "sftp://u@h:2222/srv/x",
        "ftp://h/pub",
        "s3://bucket/key/part",
        "gs://bucket/key",
        "az://acct/container/blob",
        "data:text/plain;base64,YWJj",
        "file:///tmp/x",
        "github://github.com/o/r/dir/f.txt",
        "unknown://h/p/q",
        "a/relative/path",
    ],
)
def test_a_path_pickles_as_the_same_value(url, protocol):
    path = UriPath(url)
    again = _roundtrip(path, protocol)
    assert type(again) is type(path)
    assert again == path
    assert hash(again) == hash(path)
    assert again.as_uri(sanitize=False) == path.as_uri(sanitize=False)
    assert again.parts == path.parts


def test_a_path_that_was_used_pickles_the_same_as_one_that_was_not():
    fresh = UriPath("ftp://h/pub/x")
    used = UriPath("ftp://h/pub/x")
    used.backend
    assert pickle.dumps(used) == pickle.dumps(fresh)


def test_a_path_with_userinfo_pickles_with_it_and_no_more():
    path = UriPath("sftp://user:S3CR3T-dummy@h/x")
    data = pickle.dumps(path)
    assert SECRET in data  # the URI text itself carries it
    assert _roundtrip(path).source.parsed_userinfo() == ("user", "S3CR3T-dummy")
    assert SECRET not in pickle.dumps(UriPath("sftp://user@h/x"))


def test_an_escaped_colon_in_the_user_name_survives_a_pickle():
    path = UriPath("sftp://us%3Aer:pw@h/x")
    assert _roundtrip(path).source.parsed_userinfo() == ("us:er", "pw")
    assert _roundtrip(path.source).parsed_userinfo() == ("us:er", "pw")


def test_a_supplied_backend_is_not_in_the_pickle():
    backend = _Backend()
    path = UriPath("sftp://h/x", backend=backend)
    data = pickle.dumps(path)
    assert SECRET not in data
    again = pickle.loads(data)
    assert again == path
    assert again._supplied_backend() is None


def test_a_backend_that_declares_itself_picklable_goes_along():
    path = UriPath("sftp://h/x", backend=_PicklableBackend("mine"))
    again = _roundtrip(path)
    assert again == path
    assert again._supplied_backend() == _PicklableBackend("mine")


def test_a_derived_backend_is_not_pickled_and_is_built_again():
    pytest.importorskip("requests")
    path = UriPath("http://h/x")
    backend = path.backend
    again = _roundtrip(path)
    assert again._backend is None
    assert again.backend is not backend
    assert again._supplied_backend() is None


def test_a_pickle_keeps_the_allow_list_of_schemes():
    from pathlib_next.uri.schemes import DataUri

    path = UriPath("data:,abc", schemesmap={"data": DataUri})
    again = _roundtrip(path)
    assert again._schemes_in_use == {"data": DataUri}
    assert type(again / UriPath("file:///x")) is UriPath


def test_the_stat_hint_and_the_file_object_cache_are_not_pickled(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    (child,) = UriPath(tmp_path.as_uri()).iterdir()
    assert child._stat_hint is not None
    child.filepath
    again = _roundtrip(child)
    assert again._stat_hint is None
    assert again == child
    assert again.read_text() == "x"


# --- per scheme: no session, no client, no credential -----------------------------------


def test_an_http_path_pickles_without_its_session_headers():
    requests = pytest.importorskip("requests")
    path = UriPath("http://h/x").with_session(
        requests.Session(), headers={"Authorization": "Bearer S3CR3T-dummy"}
    )
    assert path._supplied_backend() is not None
    data = pickle.dumps(path)
    assert SECRET not in data
    assert _roundtrip(path) == UriPath("http://h/x")


def test_an_sftp_path_pickles_without_the_connect_options():
    pytest.importorskip("paramiko")
    from pathlib_next.uri.schemes.sftp import SftpBackend

    backend = SftpBackend(connect_opts={"password": "S3CR3T-dummy"})
    path = UriPath("sftp://u@h/x", backend=backend)
    data = pickle.dumps(path)
    assert SECRET not in data
    assert _roundtrip(path) == UriPath("sftp://u@h/x")


def test_an_sftp_path_keeps_its_ssh_config_through_a_pickle_and_a_copy():
    from pathlib_next.uri.schemes.sftp import SftpPath
    from pathlib_next.uri.schemes.sftp._sshconfig import _DEFAULT_SSH_CONFIG

    custom = SftpPath("sftp://h/x", ssh_config=None)
    default = SftpPath("sftp://h/x")
    assert default._ssh_config is _DEFAULT_SSH_CONFIG
    for convert in (_roundtrip, copy.copy, copy.deepcopy):
        assert convert(custom)._ssh_config is None
        assert convert(default)._ssh_config is _DEFAULT_SSH_CONFIG
    assert _roundtrip(SftpPath("sftp://h/x", ssh_config=["a", "b"]))._ssh_config == [
        "a",
        "b",
    ]


def test_an_s3_path_pickles_without_client_kwargs_or_its_live_client(monkeypatch):
    pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws

    from pathlib_next.uri.schemes.s3 import S3Backend

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    with mock_aws():
        backend = S3Backend(
            region_name="us-east-1",
            aws_access_key_id="AKIADUMMY",
            aws_secret_access_key="S3CR3T-dummy",
        )
        backend.client().create_bucket(Bucket="bkt")
        path = UriPath("s3://bkt/key", backend=backend)
        path.write_bytes(b"data")
        assert path.exists()
        assert backend._client is not None  # a live client exists now
        data = pickle.dumps(path)
        assert SECRET not in data
        again = pickle.loads(data)
        assert again == path
        assert again._supplied_backend() is None
        assert path.read_bytes() == b"data"


def test_a_zip_path_pickles_before_and_after_it_was_used(tmp_path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("m.txt", "member")
    member = UriPath(f"zip:{archive.as_uri()}!/m.txt")
    before = _roundtrip(member)
    assert member.read_text() == "member"
    after = _roundtrip(member)
    for again in (before, after):
        assert again == member
        assert again.read_text() == "member"
        assert again._node_key() == member._node_key()


# --- copy and deepcopy ---------------------------------------------------------------------


@pytest.mark.parametrize("copier", [copy.copy, copy.deepcopy])
def test_a_copy_shares_a_supplied_backend_and_never_duplicates_it(copier):
    backend = _Backend()
    path = UriPath("sftp://h/x", backend=backend)
    again = copier(path)
    assert again is not path
    assert again == path
    assert again._backend is backend
    assert again._supplied_backend() is backend
    assert backend.copies == 0
    assert again._same_filesystem(path) and path._same_filesystem(again)


@pytest.mark.parametrize("copier", [copy.copy, copy.deepcopy])
def test_a_copy_of_a_path_with_a_derived_backend_still_reads_as_derived(copier):
    path = UriPath("ftp://h/x")
    backend = path.backend
    again = copier(path)
    assert again._backend is backend
    assert again._supplied_backend() is None
    other = UriPath("ftp://h/x")
    other.backend
    assert path._same_filesystem(other)
    assert again._same_filesystem(other)


@pytest.mark.parametrize("copier", [copy.copy, copy.deepcopy])
def test_a_copy_does_not_carry_the_stat_hint(copier, tmp_path):
    (tmp_path / "f.txt").write_text("x")
    (child,) = UriPath(tmp_path.as_uri()).iterdir()
    assert child._stat_hint is not None
    assert copier(child)._stat_hint is None


def test_a_dataclass_holding_paths_can_be_turned_into_a_dict(tmp_path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("m.txt", "member")

    @dataclasses.dataclass
    class Config:
        archive_member: UriPath
        remote: UriPath

    config = Config(UriPath(f"zip:{archive.as_uri()}!/m.txt"), UriPath("ftp://h/x"))
    config.archive_member.read_text()
    config.remote.backend
    converted = dataclasses.asdict(config)
    assert converted["archive_member"] == config.archive_member
    assert converted["remote"] == config.remote
    assert converted["remote"]._same_filesystem(config.remote)
    assert converted["archive_member"].read_text() == "member"


def test_a_copy_of_a_plain_uri_is_a_value_too():
    uri = Uri("http://u:pw@h/a?x=1#f")
    assert copy.deepcopy(uri) == uri
    assert _roundtrip(uri) == uri
    assert _roundtrip(uri).source == uri.source


# --- identity survives, per scheme ----------------------------------------------------------


_IDENTITY_URLS = [
    "http://h/x",
    "https://h/x",
    "dav://h/x",
    "ftp://h/x",
    "s3://bkt/x",
    "gs://bkt/x",
    "az://acct/c/x",
    "file:///tmp/x",
    "data:,abc",
]


@pytest.mark.parametrize("url", _IDENTITY_URLS)
@pytest.mark.parametrize("how", ["pickle", "deepcopy", "copy"])
def test_equality_hash_and_filesystem_answers_survive(url, how):
    pytest.importorskip("requests")
    first, second = UriPath(url), UriPath(url)
    for path in (first, second):
        try:
            path.backend
        except ImportError:
            pytest.skip("the client library for this scheme is not installed")
    expected = (
        first == second,
        hash(first) == hash(second),
        first._same_filesystem(second),
    )
    convert = {
        "pickle": _roundtrip,
        "deepcopy": copy.deepcopy,
        "copy": copy.copy,
    }[how]
    first2, second2 = convert(first), convert(second)
    assert (
        first2 == second2,
        hash(first2) == hash(second2),
        first2._same_filesystem(second2),
    ) == expected
    assert first2 == first and hash(first2) == hash(first)
    assert first2._same_filesystem(first) and first._same_filesystem(first2)
    assert first2._node_key() == first._node_key()


def test_two_distinct_supplied_backends_stay_distinct_through_a_copy():
    one, two = _Backend(), _Backend()
    a = UriPath("sftp://h/x", backend=one)
    b = UriPath("sftp://h/x", backend=two)
    assert not a._same_filesystem(b)
    assert not copy.copy(a)._same_filesystem(copy.deepcopy(b))
    assert copy.copy(a)._same_filesystem(a)


def test_source_with_a_split_userinfo_copies_and_pickles():
    source = Source("sftp", "u", "h", 22)
    assert copy.deepcopy(source) == source
    assert _roundtrip(source) == source
