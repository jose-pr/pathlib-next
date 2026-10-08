"""What `github:`, `gitlab:` and `git:` paths show of a token, how they compose
the API URL a request would use, and when their backend warns. No test here
reaches a server: the backends are stubs that record the call."""

import urllib.parse
import warnings

import pytest

requests = pytest.importorskip("requests")

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes._gitrepo import (
    BaseRepoBackend,
    InsecureTransportWarning,
    RepoBackend,
)

API = "https://api.example.test/base"


class _NoRequests(BaseRepoBackend):
    """A backend whose API root is `API` and which fails the test on use."""

    __slots__ = ("api_base", "cache")

    def __init__(self, api_base=API):
        self.api_base = api_base
        self.cache = {}

    def request(self, method, url, **kwargs):
        pytest.fail(f"a request was made: {method} {url}")


def _github(uri):
    return UriPath(uri, backend=_NoRequests())


# --- a token is not shown ---


@pytest.mark.parametrize(
    "uri",
    [
        "github://SECRETTOKEN@github.com/o/r/f",
        "github://x-access-token:SECRETTOKEN@github.com/o/r/f",
        "gitlab://oauth2:SECRETTOKEN@gitlab.com/o/r/f",
        "gitlab://SECRETTOKEN@gitlab.com/g/s/p/-/f",
        "git://SECRETTOKEN@github.com/o/r/f",
        "git+github://SECRETTOKEN@ghe.example/o/r/f",
    ],
)
def test_no_display_form_of_a_provider_path_shows_the_token(uri):
    path = UriPath(uri)
    for shown in (
        str(path),
        repr(path),
        format(path),
        path.as_posix(),
        path.parent.as_posix(),
        path.as_uri(sanitize=True),
        repr(path.source),
        str(path.source),
    ):
        assert "SECRETTOKEN" not in shown
    assert path.as_posix() == f"{path.source.host}:{path.path}"


def test_other_schemes_keep_their_user_in_as_posix():
    assert UriPath("sftp://alice:pw@h/x").as_posix() == "alice@h:/x"


# --- owner, repository and path are one URL component each ---


@pytest.mark.parametrize(
    "uri, expected",
    [
        (
            "github://github.com/o/r%3Fx=1/a.txt",
            f"{API}/repos/o/r%3Fx%3D1/contents/a.txt",
        ),
        (
            "github://github.com/o/r%23frag/dir%2Fname/a%3Fb.txt",
            f"{API}/repos/o/r%23frag/contents/dir/name/a%3Fb.txt",
        ),
        (
            "github://github.com/o/r/a b/c%20d.txt",
            f"{API}/repos/o/r/contents/a%20b/c%20d.txt",
        ),
    ],
)
def test_github_api_url_encodes_owner_repo_and_path(uri, expected):
    assert _github(uri)._contents_url() == expected


@pytest.mark.parametrize(
    "uri",
    [
        "github://github.com/o/r/a/%2E%2E/%2E%2E/%2E%2E/%2E%2E/%2E%2E/user",
        "github://github.com/o/..%2F..%2Fuser%2Femails%23/x",
        "github://github.com/o%2F..%2F..%2Fnotifications%3Fall=true%23/r/a",
        "github://github.com/o/r/a%2F..%2F..%2F..%2Fuser",
    ],
)
def test_github_api_url_of_crafted_uri_text_stays_under_repos(uri):
    url = _github(uri)._contents_url()
    parts = urllib.parse.urlsplit(url)
    assert parts.query == "" and parts.fragment == ""
    segments = parts.path.split("/")
    assert "." not in segments and ".." not in segments
    assert url.startswith(f"{API}/repos/")


@pytest.mark.parametrize(
    "build",
    [
        lambda p: p.with_name(".."),
        lambda p: p.with_path("/o/r/../../x"),
        lambda p: p.with_segments("o/r", "..", "x"),
        lambda p: p._make_child_relpath(".."),
        lambda p: p.with_path("/o/r/a/./b"),
    ],
    ids=["with_name", "with_path", "with_segments", "child", "dot"],
)
def test_github_dot_segment_cannot_reach_the_api_url(build):
    path = build(_github("github://github.com/o/r/a"))
    with pytest.raises(ValueError):
        path._contents_url()
    # `exists()` answers a bool and sends nothing (the backend would fail).
    assert path.exists() is False


def test_github_owner_or_repo_that_is_a_dot_segment_is_refused():
    path = _github("github://github.com/o/r/a").with_path("/../r/a")
    with pytest.raises(ValueError):
        path._repo_url


def test_github_tree_url_refuses_a_ref_that_is_a_dot_segment():
    path = _github("github://github.com/o/r/dir?ref=../../x")
    with pytest.raises(ValueError):
        path._tree_entries(path.ref)


@pytest.mark.parametrize("path_text", ["..", "a/..", "./x"])
def test_gitlab_dot_segment_cannot_reach_the_file_url(path_text):
    path = UriPath("gitlab://gitlab.com/o/r", backend=_NoRequests(f"{API}/api/v4"))
    with pytest.raises(ValueError):
        path._file_url(path_text)


def test_gitlab_file_url_is_one_encoded_segment():
    path = UriPath("gitlab://gitlab.com/o/r", backend=_NoRequests(f"{API}/api/v4"))
    assert path._file_url("a b/c#d.txt", "/raw") == (
        f"{API}/api/v4/projects/o%2Fr/repository/files/a%20b%2Fc%23d.txt/raw"
    )


# --- a token over plain http warns, except to this machine ---


class _Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("headers")))
        return object()


def _send(api_base, **backend_args):
    session = _Session()
    backend = RepoBackend(session=session, api_base=api_base, **backend_args)
    backend.request("GET", f"{backend.api_base}/repos/o/r")
    return session.calls


def test_token_over_plain_http_to_another_host_warns_and_is_still_sent():
    with pytest.warns(InsecureTransportWarning, match="api.example.test"):
        calls = _send("http://api.example.test/api", token="T")
    assert calls[0][2]["Authorization"] == "Bearer T"


def test_authorization_header_over_plain_http_warns():
    with pytest.warns(InsecureTransportWarning):
        _send("http://api.example.test", headers={"authorization": "Token T"})


@pytest.mark.parametrize(
    "api_base",
    [
        "http://127.0.0.1:8080",
        "http://localhost:8080/api",
        "http://[::1]:8080",
        "http://api.localhost",
        "https://api.example.test",
    ],
)
def test_token_to_loopback_or_https_does_not_warn(api_base):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _send(api_base, token="T")


def test_plain_http_without_a_credential_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _send("http://api.example.test")


def test_the_warning_is_a_userwarning_the_header_names():
    import pathlib

    import pathlib_next

    assert issubclass(InsecureTransportWarning, UserWarning)
    header = (pathlib.Path(pathlib_next.__file__).parent / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    assert "InsecureTransportWarning" in header
