"""What `github:` and `gitlab:` do with API replies that are not the shape
the API documents: a captive portal's page, a gateway's JSON, `null`, a
truncated body. Each is `OSError(EIO)` naming the path, never an
`AttributeError`, `KeyError` or `requests` exception, and `exists()` answers
`False`. Every request goes to a loopback server."""

import base64
import errno
import json

import pytest

requests = pytest.importorskip("requests")

import http.server

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes._gitrepo import RepoBackend
from pathlib_next.uri.schemes.github import GitHubPath
from pathlib_next.uri.schemes.gitlab import GitLabPath


class _Api:
    """A loopback API: `route(path_with_query)` returns `(status, headers,
    body)`. Every request is logged in `log` as `(method, path)`."""

    def __init__(self):
        self.log = []
        self.route = lambda path: (404, {}, b"{}")


def _json(payload, status=200, headers=None):
    body = json.dumps(payload).encode()
    return status, {"Content-Type": "application/json", **(headers or {})}, body


def _handler(api):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _serve(self):
            api.log.append((self.command, self.path))
            status, headers, body = api.route(self.path)
            self.send_response(status)
            for key, value in {"Content-Length": str(len(body)), **headers}.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = _serve

    return Handler


@pytest.fixture
def api(serve_http):
    api = _Api()
    with serve_http(_handler(api)) as base:
        api.base = base
        yield api


def _github(api, name="dir/f.txt", query=""):
    return UriPath(
        f"github://github.com/o/r/{name}{query}",
        backend=RepoBackend(api_base=api.base),
    )


def _gitlab(api, name="dir/f.txt", query="?ref=main"):
    return UriPath(
        f"gitlab://gitlab.com/o/r/{name}{query}",
        backend=RepoBackend(api_base=api.base + "/api/v4"),
    )


# what a proxy, a portal or a broken server may answer with status 200
_NOT_JSON = {
    "html": (200, {"Content-Type": "text/html"}, b"<html>login</html>"),
    "empty": (200, {"Content-Type": "application/json"}, b""),
    "truncated": (200, {"Content-Type": "application/json"}, b'[{"name": "a", "ty'),
}
_WRONG_TYPE = {
    "null": _json(None),
    "string": _json("nope"),
    "number": _json(5),
}
_ALL_BAD = {**_NOT_JSON, **_WRONG_TYPE}


def _assert_eio(error, path):
    assert error.errno == errno.EIO, error
    assert not isinstance(error, requests.exceptions.RequestException)
    assert str(path) in str(error)


# --- GitHub ---


@pytest.mark.parametrize(
    "reply",
    [
        *_ALL_BAD.values(),
        _json({"message": "hello"}),
        _json({"type": "file", "size": "big"}),
        _json({"type": "file", "size": -1}),
    ],
    ids=[*_ALL_BAD, "object-without-type", "size-text", "size-negative"],
)
def test_github_stat_of_a_reply_that_is_not_a_contents_object_is_eio(api, reply):
    api.route = lambda path: reply
    path = _github(api)
    with pytest.raises(OSError) as raised:
        path.stat()
    _assert_eio(raised.value, path)
    assert path.exists() is False
    assert path.is_file() is False


@pytest.mark.parametrize(
    "reply",
    [
        *_ALL_BAD.values(),
        _json(["a", "b"]),
        _json([{"path": "a"}]),
        _json([{"name": "a", "type": "file", "size": "x"}]),
        _json({"message": "hello"}),
    ],
    ids=[*_ALL_BAD, "strings", "no-name-or-type", "size-text", "object-without-type"],
)
def test_github_listing_of_a_reply_that_is_not_a_directory_is_eio(api, reply):
    api.route = lambda path: reply
    path = _github(api, "dir")
    with pytest.raises(OSError) as raised:
        list(path.iterdir())
    _assert_eio(raised.value, path)


def test_github_listing_of_a_file_object_is_still_not_a_directory(api):
    api.route = lambda path: _json({"type": "file", "size": 3})
    with pytest.raises(NotADirectoryError):
        list(_github(api, "dir").iterdir())


@pytest.mark.parametrize(
    "reply",
    [
        _json(None),
        _json("x"),
        _json({"type": "file", "encoding": "base64"}),
        _json({"type": "file", "encoding": "base64", "content": "not base64!"}),
        _json({"type": "file", "encoding": "base64", "content": 5}),
        _NOT_JSON["empty"],
        _NOT_JSON["truncated"],
    ],
)
def test_github_read_of_a_json_envelope_that_is_not_a_file_is_eio(api, reply):
    api.route = lambda path: reply
    path = _github(api)
    with pytest.raises(OSError) as raised:
        path.read_bytes()
    _assert_eio(raised.value, path)


def test_github_read_of_a_json_envelope_still_decodes(api):
    content = base64.b64encode(b"hello").decode()
    api.route = lambda path: _json(
        {"type": "file", "encoding": "base64", "content": content}
    )
    assert _github(api).read_bytes() == b"hello"
    api.route = lambda path: _json([{"name": "a", "type": "file"}])
    with pytest.raises(IsADirectoryError):
        _github(api).read_bytes()


def _capped(tree_reply, repo_reply=None):
    """A `/dir` of 1,000 entries (the contents API cap), so the listing is
    re-read through the trees API with `tree_reply`."""
    entries = [{"name": f"f{i}", "type": "file", "size": 1} for i in range(1000)]

    def route(path):
        if "/git/trees/" in path:
            return tree_reply
        if path.split("?")[0].endswith("/contents/dir"):
            return _json([{"name": "dir", "type": "dir", "sha": "S"}] * 1000)
        if path.split("?")[0].endswith("/contents"):
            return _json([{"name": "dir", "type": "dir", "sha": "S", "size": 0}])
        if path.split("?")[0].endswith("/repos/o/r"):
            return repo_reply or _json({"default_branch": "main"})
        return _json(entries)

    return route


@pytest.mark.parametrize(
    "tree_reply",
    [
        *_ALL_BAD.values(),
        _json({"sha": "x"}),
        _json({"tree": "x"}),
        _json({"tree": [{"type": "blob"}]}),
        _json({"tree": [{"path": "a", "type": "blob", "size": "x"}]}),
        _json([1, 2]),
    ],
    ids=[
        *_ALL_BAD,
        "no-tree",
        "tree-text",
        "entry-without-path",
        "size-text",
        "list",
    ],
)
def test_github_trees_reply_that_is_not_a_tree_is_eio(api, tree_reply):
    api.route = _capped(tree_reply)
    path = _github(api, "dir")
    with pytest.raises(OSError) as raised:
        list(path.iterdir())
    _assert_eio(raised.value, path)


@pytest.mark.parametrize(
    "repo_reply",
    [*_ALL_BAD.values(), _json({"message": "x"}), _json({"default_branch": 7})],
    ids=[*_ALL_BAD, "no-branch", "branch-number"],
)
def test_github_default_branch_reply_that_is_not_a_repository_is_eio(api, repo_reply):
    # The root listing at the cap needs the default branch for its tree.
    def route(path):
        if path.split("?")[0].endswith("/repos/o/r"):
            return repo_reply
        return _json([{"name": f"f{i}", "type": "file"} for i in range(1000)])

    api.route = route
    path = _github(api, "")
    with pytest.raises(OSError) as raised:
        list(path.iterdir())
    _assert_eio(raised.value, path)


def test_github_truncated_tree_is_still_eio(api):
    api.route = _capped(_json({"tree": [], "truncated": True}))
    with pytest.raises(OSError) as raised:
        list(_github(api, "dir").iterdir())
    assert raised.value.errno == errno.EIO


# --- GitLab ---


@pytest.mark.parametrize(
    "reply",
    [*_ALL_BAD.values(), _json({"message": "x"}), _json({"size": "big"})],
    ids=[*_ALL_BAD, "object", "size-text"],
)
def test_gitlab_stat_of_file_metadata_that_is_not_a_file_is_eio(api, reply):
    # The HEAD reply carries no size, so the metadata is asked for.
    api.route = lambda path: reply
    path = _gitlab(api)
    with pytest.raises(OSError) as raised:
        path.stat()
    _assert_eio(raised.value, path)
    assert path.exists() is False


@pytest.mark.parametrize(
    "reply",
    [*_ALL_BAD.values(), _json({"message": "x"}), _json({"default_branch": ""})],
    ids=[*_ALL_BAD, "no-branch", "empty-branch"],
)
def test_gitlab_default_branch_reply_that_is_not_a_project_is_eio(api, reply):
    api.route = lambda path: reply
    path = _gitlab(api, query="")
    with pytest.raises(OSError) as raised:
        path.stat()
    _assert_eio(raised.value, path)
    assert path.exists() is False


@pytest.mark.parametrize(
    "reply",
    [
        *_ALL_BAD.values(),
        _json({"message": "x"}),
        _json(["a"]),
        _json([{"path": "a"}]),
    ],
    ids=[*_ALL_BAD, "object", "strings", "no-name-or-type"],
)
def test_gitlab_tree_reply_that_is_not_a_tree_is_eio(api, reply):
    api.route = lambda path: reply
    path = _gitlab(api, "dir")
    with pytest.raises(OSError) as raised:
        list(path.iterdir())
    _assert_eio(raised.value, path)


@pytest.mark.parametrize(
    "reply",
    [*_ALL_BAD.values(), _json({"message": "x"})],
    ids=[*_ALL_BAD, "object"],
)
def test_gitlab_stat_of_the_root_needs_a_tree_array(api, reply):
    api.route = lambda path: reply
    path = _gitlab(api, "")
    with pytest.raises(OSError) as raised:
        path.stat()
    _assert_eio(raised.value, path)
    assert path.exists() is False


# --- GitLab asks for a file's size without its content ---


def _files_route(size=5, head=None):
    def route(path):
        if "/repository/files/" in path:
            headers = {"X-Gitlab-Size": str(size)}
            return (
                _json({"size": size}, headers=headers) if head is None else head(path)
            )
        return _json([])

    return route


def test_gitlab_stat_of_a_file_is_a_head_request(api):
    api.route = _files_route(size=7)
    assert _gitlab(api, "dir/a b.txt").stat().st_size == 7
    assert [m for m, _p in api.log] == ["HEAD"]
    assert api.log[0][1].startswith("/api/v4/projects/o%2Fr/repository/files/")
    assert "dir%2Fa%20b.txt?ref=main" in api.log[0][1]


@pytest.mark.parametrize(
    "head",
    [
        lambda path: (405, {}, b""),
        lambda path: (200, {}, b""),
        lambda path: (200, {"X-Gitlab-Size": "abc"}, b""),
    ],
    ids=["refused", "no-size", "bad-size"],
)
def test_gitlab_stat_falls_back_to_the_metadata_without_a_usable_head(api, head):
    def route(path):
        if "/repository/files/" in path:
            if api.log[-1][0] == "HEAD":
                return head(path)
            return _json({"size": 9})
        return _json([])

    api.route = route
    assert _gitlab(api).stat().st_size == 9
    assert [m for m, _p in api.log] == ["HEAD", "GET"]


def test_gitlab_stat_of_a_missing_file_is_not_found_after_one_head_and_one_tree(api):
    api.route = lambda path: _json({"message": "404"}, status=404)
    with pytest.raises(FileNotFoundError):
        _gitlab(api).stat()
    assert [m for m, _p in api.log] == ["HEAD", "GET"]
    assert "/repository/tree" in api.log[1][1]


# --- the two providers list through _scandir only ---


@pytest.mark.parametrize("cls", [GitHubPath, GitLabPath])
def test_provider_listing_is_scandir_alone(cls):
    assert "_listdir" not in vars(cls)
