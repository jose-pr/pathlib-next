"""Scheme-specific coverage for `github:`/`gitlab:` beyond the shared
`ReadPathContract` wiring in test_contract.py: ref plumbing (including
through `iterdir()`, which the base `Uri._make_child_relpath` would
otherwise silently drop -- see `_gitrepo.py`'s `_RepoApiPath._make_child_relpath`),
error translation (404/401/403/rate-limit), owner/repo/ref parsing, the
GitHub Enterprise API-base split, and GitLab's dir-vs-file stat
disambiguation.
"""

import errno
import http.server
import json

import pytest

pytest.importorskip("requests")

from pathlib_next.uri.schemes.github import GitHubPath, RepoBackend
from pathlib_next.uri.schemes.git import GitHubGitPath, GitLabGitPath, GitPath
from pathlib_next.uri.schemes.gitlab import GitLabPath
from pathlib_next.uri import UriPath


def _github(server, path="", **kwargs):
    base_url, owner, repo = server
    backend = kwargs.pop("backend", None) or RepoBackend(api_base=base_url)
    uri = f"github://github.com/{owner}/{repo}"
    if path:
        uri += f"/{path}"
    return GitHubPath(uri, backend=backend, **kwargs)


def _gitlab(server, path="", **kwargs):
    base_url, owner, repo = server
    backend = kwargs.pop("backend", None) or RepoBackend(api_base=f"{base_url}/api/v4")
    uri = f"gitlab://gitlab.com/{owner}/{repo}"
    if path:
        uri += f"/{path}"
    return GitLabPath(uri, backend=backend, **kwargs)


# --- owner/repo/ref parsing (no network) -----------------------------


def test_owner_repo_repo_path_parsing():
    p = GitHubPath("github://github.com/acme/widgets/src/pkg/mod.py")
    assert p.owner == "acme"
    assert p.repo == "widgets"
    assert p.repo_path == "src/pkg/mod.py"


def test_ref_from_query():
    p = GitHubPath("github://github.com/acme/widgets/a.txt?ref=v1.2.3")
    assert p.ref == "v1.2.3"


def test_ref_defaults_to_none():
    p = GitHubPath("github://github.com/acme/widgets/a.txt")
    assert p.ref is None


def test_github_enterprise_api_base():
    p = GitHubPath("github://ghe.internal/acme/widgets")
    assert p._api_base == "https://ghe.internal/api/v3"


def test_github_public_api_base():
    p = GitHubPath("github://github.com/acme/widgets")
    assert p._api_base == "https://api.github.com"


def test_gitlab_api_base_always_v4():
    p = GitLabPath("gitlab://gitlab.example.com/acme/widgets")
    assert p._api_base == "https://gitlab.example.com/api/v4"


def test_git_scheme_dispatches_by_public_host():
    assert type(UriPath("git://github.com/acme/widgets")) is GitHubPath
    assert type(UriPath("git://WWW.GitHub.COM/acme/widgets")) is GitHubPath
    assert type(UriPath("git://gitlab.com/acme/widgets")) is GitLabPath
    assert type(UriPath("git+github://github.com/acme/widgets")) is GitHubGitPath
    assert type(UriPath("git+gitlab://gitlab.com/acme/widgets")) is GitLabGitPath


def test_git_scheme_explicit_hosts_use_provider_api_bases():
    assert (
        UriPath("git+github://ghe.internal/acme/widgets")._api_base
        == "https://ghe.internal/api/v3"
    )
    assert (
        UriPath("git+gitlab://gitlab.internal/acme/widgets")._api_base
        == "https://gitlab.internal/api/v4"
    )


@pytest.mark.parametrize("uri", ["git://ghe.internal/acme/widgets", "git:"])
def test_git_scheme_raises_for_ambiguous_hosts(uri):
    with pytest.raises(ValueError) as excinfo:
        UriPath(uri)
    message = str(excinfo.value)
    assert "git+github:" in message
    assert "git+gitlab:" in message


def test_token_from_userinfo():
    p = GitHubPath("github://mytoken@github.com/acme/widgets")
    assert p.backend.token == "mytoken"


def test_backend_kwarg_wins_over_userinfo():
    backend = RepoBackend(token="explicit")
    p = GitHubPath("github://ignored@github.com/acme/widgets", backend=backend)
    assert p.backend.token == "explicit"


# --- ref plumbing end-to-end, including through iterdir() ------------


def test_github_ref_selects_branch_content(github_api_server):
    main_content = _github(github_api_server, "a.txt").read_bytes()
    other_content = _github(github_api_server, "a.txt?ref=other-branch").read_bytes()
    assert main_content == b"a"
    assert other_content == b"a-on-other-branch"


def test_github_ref_survives_iterdir_child(github_api_server):
    root = _github(github_api_server, "?ref=other-branch")
    child = next(p for p in root.iterdir() if p.name == "a.txt")
    assert child.ref == "other-branch"
    assert child.read_bytes() == b"a-on-other-branch"


def test_github_ref_survives_nested_iterdir(github_api_server):
    root = _github(github_api_server, "?ref=other-branch")
    sub = next(p for p in root.iterdir() if p.name == "sub")
    nested = next(p for p in sub.iterdir() if p.name == "nested")
    assert nested.ref == "other-branch"


# --- GitHub-specific behavior ------------------------------------------


def test_github_scandir_on_file_raises_not_a_directory(github_api_server):
    p = _github(github_api_server, "a.txt")
    with pytest.raises(NotADirectoryError):
        list(p._scandir())


def test_github_open_on_directory_raises_is_a_directory(github_api_server):
    p = _github(github_api_server, "sub")
    with pytest.raises(IsADirectoryError):
        p.read_bytes()


def test_github_symlink_entry_treated_as_file(serve_http):
    """A `symlink`/`submodule` contents-API entry has no special handling
    -- it's surfaced as a plain file (documented divergence)."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(
                [{"name": "link", "path": "link", "type": "symlink", "size": 4}]
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    with serve_http(_Handler) as base_url:
        backend = RepoBackend(api_base=base_url)
        p = GitHubPath("github://github.com/acme/widgets", backend=backend)
        entries = dict(p._scandir())
        assert not entries["link"].is_dir()
        assert entries["link"].st_size == 4


# --- GitLab-specific behavior --------------------------------------------


def test_gitlab_stat_missing_path_raises_file_not_found(gitlab_api_server):
    p = _gitlab(gitlab_api_server, "nope.txt")
    with pytest.raises(FileNotFoundError):
        p.stat()


def test_gitlab_scandir_blob_entries_have_no_stat_hint(gitlab_api_server):
    p = _gitlab(gitlab_api_server)
    entries = dict(p._scandir())
    assert entries["a.txt"] is None
    assert entries["sub"].is_dir()


def test_gitlab_nested_directory_stat(gitlab_api_server):
    p = _gitlab(gitlab_api_server, "sub/nested")
    assert p.stat().is_dir()


def test_git_scheme_ref_survives_iterdir_child(github_api_server):
    backend = RepoBackend(api_base=github_api_server[0])
    root = UriPath("git://github.com/acme/widgets?ref=other-branch", backend=backend)
    child = next(p for p in root.iterdir() if p.name == "a.txt")
    assert type(child) is GitHubPath
    assert child.ref == "other-branch"
    assert child.read_bytes() == b"a-on-other-branch"


def test_git_scheme_token_auth_works():
    p = UriPath("git://TOKEN@github.com/acme/widgets")
    assert p.backend.token == "TOKEN"


def test_gitpath_direct_constructor_initializes_provider_instance():
    p = GitPath("git://github.com/acme/widgets/a.txt?ref=dev")
    assert type(p) is GitHubPath
    assert p.owner == "acme"
    assert p.repo == "widgets"
    assert p.ref == "dev"
    assert p.as_uri() == "git://github.com/acme/widgets/a.txt?ref=dev"


# --- error translation ----------------------------------------------------


@pytest.fixture
def status_server(serve_http):
    """Serves canned status codes on demand -- `/<code>` returns that
    status, `/ratelimited` returns 403 with GitHub's rate-limit headers.
    """

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.lstrip("/").split("?", 1)[0]
            first_segment = path.split("/", 1)[0]
            if first_segment == "ratelimited":
                self.send_response(403)
                self.send_header("X-RateLimit-Remaining", "0")
                self.send_header("X-RateLimit-Reset", "1234567890")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                code = int(first_segment)
            except ValueError:
                code = 404
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    with serve_http(_Handler) as base_url:
        yield base_url


def test_github_404_raises_file_not_found(status_server):
    backend = RepoBackend(api_base=f"{status_server}/404")
    p = GitHubPath("github://github.com/acme/widgets", backend=backend)
    with pytest.raises(FileNotFoundError):
        p.stat()


def test_github_403_without_rate_limit_headers_raises_permission_error(status_server):
    backend = RepoBackend(api_base=f"{status_server}/403")
    p = GitHubPath("github://github.com/acme/widgets", backend=backend)
    with pytest.raises(PermissionError):
        p.stat()


def test_github_rate_limit_raises_clear_oserror(status_server):
    backend = RepoBackend(api_base=f"{status_server}/ratelimited")
    p = GitHubPath("github://github.com/acme/widgets", backend=backend)
    with pytest.raises(OSError) as excinfo:
        p.stat()
    assert excinfo.value.errno == errno.EAGAIN
    assert "rate limit" in str(excinfo.value)


# --- gittools-api-base-drops-port-and-brackets ---


@pytest.mark.parametrize(
    "uri, expected",
    [
        ("gitlab://gitlab.internal:8929/o/r", "https://gitlab.internal:8929/api/v4"),
        ("gitlab://[fd00::5]/o/r", "https://[fd00::5]/api/v4"),
        ("gitlab://[fd00::5]:8443/o/r", "https://[fd00::5]:8443/api/v4"),
        ("github://ghe.internal:8443/o/r", "https://ghe.internal:8443/api/v3"),
        ("github://[fd00::5]/o/r", "https://[fd00::5]/api/v3"),
        ("github://github.com/o/r", "https://api.github.com"),
    ],
)
def test_api_base_keeps_port_and_brackets(uri, expected):
    assert UriPath(uri)._api_base == expected


def test_repo_backend_headers_merge_with_request_headers():
    class _Session:
        def request(self, method, url, **kwargs):
            self.kwargs = kwargs

    session = _Session()
    backend = RepoBackend(token="T", session=session, headers={"X-A": "1"})
    backend.request("GET", "http://h/x", headers={"Accept": "raw"})
    assert session.kwargs["headers"] == {
        "X-A": "1",
        "Accept": "raw",
        "Authorization": "Bearer T",
    }


# --- gittools-gitlab-tree-pagination-truncates ---


def test_gitlab_listing_over_one_page_is_complete(gitlab_api_server, fixture_tree):
    many = fixture_tree / "many"
    many.mkdir()
    for i in range(130):
        (many / f"f{i:03}.txt").write_text("x")
    for i in range(150):
        (fixture_tree / "pkgs" / f"pkg{i:03}").mkdir(parents=True)
        (fixture_tree / "pkgs" / f"pkg{i:03}" / "m.py").write_text("m")

    assert len(list(_gitlab(gitlab_api_server, "many").iterdir())) == 130
    assert len(list(_gitlab(gitlab_api_server, "many").glob("*.txt"))) == 130
    names = {p.name for p in _gitlab(gitlab_api_server, "pkgs").iterdir()}
    assert names == {f"pkg{i:03}" for i in range(150)}
    assert _gitlab(gitlab_api_server, "pkgs/pkg149").is_dir()
    assert _gitlab(gitlab_api_server, "pkgs/pkg100").stat().is_dir()


# --- gittools-gitlab-stat-answers-from-uri-shape ---


def test_gitlab_root_stat_asks_the_server(gitlab_api_server):
    base_url, _owner, _repo = gitlab_api_server
    backend = RepoBackend(api_base=f"{base_url}/api/v4")
    assert _gitlab(gitlab_api_server).is_dir()
    for uri in (
        "gitlab://gitlab.com/nobody/ghost",
        "gitlab://gitlab.com/acme",
        "gitlab://gitlab.com/acme/widgets?ref=no-such-ref",
    ):
        p = GitLabPath(uri, backend=backend)
        assert not p.exists(), uri
        assert not p.is_dir(), uri


def test_gitlab_trailing_slash_directory_stat(gitlab_api_server):
    assert _gitlab(gitlab_api_server, "sub/").is_dir()
    assert _gitlab(gitlab_api_server, "sub/nested/").stat().is_dir()
    with pytest.raises(FileNotFoundError):
        _gitlab(gitlab_api_server, "missing/").stat()


# --- gittools-gitlab-subgroups-unaddressable ---


def test_gitlab_subgroup_project_via_separator(gitlab_api_server):
    base_url, _owner, _repo = gitlab_api_server
    backend = RepoBackend(api_base=f"{base_url}/api/v4")
    p = GitLabPath("gitlab://gitlab.com/acme/team/widgets/-/sub/c.py", backend=backend)
    assert (p.owner, p.repo, p.repo_path) == ("acme/team", "widgets", "sub/c.py")
    assert p.read_bytes() == b"c"
    assert p.stat().st_size == 1

    root = GitLabPath("gitlab://gitlab.com/acme/team/widgets/-", backend=backend)
    assert root.is_dir()
    sub = next(c for c in root.iterdir() if c.name == "sub")
    assert sub.as_uri() == "gitlab://gitlab.com/acme/team/widgets/-/sub"
    assert sorted(c.name for c in sub.iterdir()) == ["c.py", "nested"]

    # Without the separator the first two segments stay owner/repo.
    two = GitLabPath("gitlab://gitlab.com/acme/widgets/sub/c.py", backend=backend)
    assert (two.owner, two.repo, two.repo_path) == ("acme", "widgets", "sub/c.py")
    sep = GitLabPath("gitlab://gitlab.com/acme/widgets/-/sub/c.py", backend=backend)
    assert (sep.owner, sep.repo, sep.repo_path) == ("acme", "widgets", "sub/c.py")


def test_gitlab_directory_named_dash_stays_addressable(gitlab_api_server, fixture_tree):
    (fixture_tree / "-").mkdir()
    (fixture_tree / "-" / "inside.txt").write_text("dash")
    root = _gitlab(gitlab_api_server)
    dash = next(c for c in root.iterdir() if c.name == "-")
    assert dash.repo_path == "-"
    assert dash.is_dir()
    assert [c.name for c in dash.iterdir()] == ["inside.txt"]
    assert next(dash.iterdir()).read_bytes() == b"dash"


# --- gittools-github-contents-1000-entry-cap ---


def test_github_listing_past_contents_cap_is_complete(github_api_server, fixture_tree):
    for i in range(1000):
        (fixture_tree / f"r{i:04}.txt").write_text("")
    wide = fixture_tree / "sub" / "nested" / "wide"
    wide.mkdir()
    for i in range(1001):
        (wide / f"w{i:04}.txt").write_text("w")

    # The root itself is capped too, so the `sub` tree SHA comes from the
    # Git Trees API listing of the root (default branch, then a named ref).
    root_names = {p.name for p in _github(github_api_server).iterdir()}
    assert len(root_names) == 1005
    assert {"sub", "a.txt"} <= root_names
    other = {p.name for p in _github(github_api_server, "?ref=other-branch").iterdir()}
    assert other == root_names

    listing = dict(_github(github_api_server, "sub/nested/wide")._scandir())
    assert len(listing) == 1001
    assert listing["w1000.txt"].st_size == 1
    assert not listing["w1000.txt"].is_dir()
    nested = dict(_github(github_api_server, "sub/nested")._scandir())
    assert nested["wide"].is_dir()


# --- gittools-gitlab-wrong-kind-errors ---------------------------------------


@pytest.fixture
def gitlab_empty_listing_server(serve_http):
    """A GitLab API whose tree endpoint answers an empty 200 for any path
    that is not a directory (a file or a missing path), as the module's
    stat() allows for, instead of the conftest fake's 404."""
    import urllib.parse

    project = "/api/v4/projects/acme%2Fwidgets"
    files = {"a.txt": b"A", "pkgs/m.py": b"M"}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(split.query)
            if split.path == project:
                return self._json(200, {"default_branch": "main"})
            if split.path == f"{project}/repository/tree":
                path = qs.get("path", [""])[0]
                prefix = f"{path}/" if path else ""
                names = sorted(
                    {
                        name[len(prefix) :].split("/", 1)[0]
                        for name in files
                        if name.startswith(prefix)
                    }
                )
                return self._json(
                    200,
                    [
                        {
                            "name": name,
                            "type": "blob" if f"{prefix}{name}" in files else "tree",
                        }
                        for name in names
                    ],
                )
            files_prefix = f"{project}/repository/files/"
            if split.path.startswith(files_prefix):
                rest = split.path[len(files_prefix) :]
                raw = rest.endswith("/raw")
                name = urllib.parse.unquote(rest[: -len("/raw")] if raw else rest)
                if name not in files:
                    return self._json(404, {"message": "404 File Not Found"})
                if raw:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(files[name])))
                    self.end_headers()
                    self.wfile.write(files[name])
                    return
                return self._json(200, {"size": len(files[name])})
            self._json(404, {})

        def _json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    with serve_http(_Handler) as base_url:
        yield base_url, "acme", "widgets"


def test_gitlab_iterdir_on_a_file_with_empty_listing_raises(
    gitlab_empty_listing_server,
):
    with pytest.raises(NotADirectoryError) as excinfo:
        list(_gitlab(gitlab_empty_listing_server, "a.txt").iterdir())
    assert excinfo.value.errno == errno.ENOTDIR


def test_gitlab_iterdir_on_a_missing_path_with_empty_listing_raises(
    gitlab_empty_listing_server,
):
    with pytest.raises(FileNotFoundError):
        list(_gitlab(gitlab_empty_listing_server, "typo_dir").iterdir())


def test_gitlab_iterdir_of_a_directory_still_lists(gitlab_empty_listing_server):
    root = _gitlab(gitlab_empty_listing_server)
    assert sorted(c.name for c in root.iterdir()) == ["a.txt", "pkgs"]
    pkgs = _gitlab(gitlab_empty_listing_server, "pkgs")
    assert [c.name for c in pkgs.iterdir()] == ["m.py"]


def test_gitlab_open_on_a_directory_raises_is_a_directory(gitlab_api_server):
    with pytest.raises(IsADirectoryError):
        _gitlab(gitlab_api_server, "sub").read_bytes()


# --- gittools-rate-limit-mapping --------------------------------------------


@pytest.fixture
def rate_limit_server(serve_http):
    """`/<status>/<header>=<value>/...` answers that status with those
    headers, for any API path below it."""
    import urllib.parse

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
            self.send_response(int(parts[0]))
            for part in parts[1:]:
                if "=" in part:
                    key, value = part.split("=", 1)
                    self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    with serve_http(_Handler) as base_url:
        yield base_url


@pytest.mark.parametrize(
    "api, detail",
    [
        ("429/Retry-After=30", "retry after 30s"),
        ("429/X-RateLimit-Remaining=0/X-RateLimit-Reset=99", "resets at 99"),
        ("403/Retry-After=60", "retry after 60s"),
        ("403/X-RateLimit-Remaining=0/X-RateLimit-Reset=7", "resets at 7"),
        ("429", "resets at ?"),
    ],
)
def test_github_primary_and_secondary_rate_limits_raise_eagain(
    rate_limit_server, api, detail
):
    backend = RepoBackend(api_base=f"{rate_limit_server}/{api}")
    p = GitHubPath("github://github.com/acme/widgets/a.txt", backend=backend)
    with pytest.raises(BlockingIOError) as excinfo:
        p.read_bytes()
    assert excinfo.value.errno == errno.EAGAIN
    assert "rate limit" in str(excinfo.value)
    assert detail in str(excinfo.value)


def test_gitlab_429_raises_eagain(rate_limit_server):
    backend = RepoBackend(
        api_base=f"{rate_limit_server}/429/RateLimit-Remaining=0/Retry-After=5"
    )
    p = GitLabPath("gitlab://gitlab.com/acme/widgets", backend=backend)
    with pytest.raises(BlockingIOError) as excinfo:
        p.stat()
    assert excinfo.value.errno == errno.EAGAIN
    assert "retry after 5s" in str(excinfo.value)


def test_plain_403_is_still_permission_error(rate_limit_server):
    backend = RepoBackend(api_base=f"{rate_limit_server}/403")
    p = GitLabPath("gitlab://gitlab.com/acme/widgets", backend=backend)
    with pytest.raises(PermissionError):
        p.stat()


# --- gittools-gitpath-dispatch-edges ------------------------------------------


@pytest.mark.parametrize(
    "uri", ["git://127.0.0.1/acme/widgets", "git://[::1]/acme/widgets"]
)
def test_git_scheme_ip_literal_host_raises_value_error(uri):
    with pytest.raises(ValueError, match="git\\+github"):
        UriPath(uri)


def test_git_scheme_with_source_keeps_provider_and_backend():
    from pathlib_next.uri import Source

    backend = RepoBackend(token="t")
    p = UriPath("git://github.com/acme/widgets/a.txt", backend=backend)
    same = p.with_source(p.source)
    assert type(same) is GitHubPath
    assert same.as_uri() == p.as_uri()
    assert same.backend is backend
    moved = p.with_source(Source("git", None, "gitlab.com", None))
    assert type(moved) is GitLabPath
    assert moved.as_uri() == "git://gitlab.com/acme/widgets/a.txt"
    assert moved.backend is not backend
    with pytest.raises(ValueError):
        p.with_source(Source("git", None, "git.example", None))


# --- gittools-custom-backend-missing-cache --------------------------------


def test_gitlab_custom_backend_without_cache_reads(gitlab_api_server, fixture_tree):
    import requests

    from pathlib_next.uri.schemes._gitrepo import BaseRepoBackend

    base_url, owner, repo = gitlab_api_server

    class _Plain(BaseRepoBackend):
        # Only `request()`: no `cache`, no `api_base` slot of the base class.
        def __init__(self):
            self.api_base = f"{base_url}/api/v4"
            self.urls = []

        def request(self, method, url, **kwargs):
            self.urls.append(url)
            return requests.request(method, url, timeout=10, **kwargs)

    backend = _Plain()
    p = GitLabPath(f"gitlab://gitlab.com/{owner}/{repo}/a.txt", backend=backend)
    assert p.read_bytes() == (fixture_tree / "a.txt").read_bytes()
    assert p.read_bytes() == (fixture_tree / "a.txt").read_bytes()
    # No memoization without a cache: the default branch is asked each time.
    assert sum(url.endswith(f"/projects/{owner}%2F{repo}") for url in backend.urls) == 2


# --- gittools-ref-query-double-decoded ---------------------------------------


def test_ref_with_escaped_ampersand_reaches_the_api_whole():
    import requests

    from pathlib_next.uri.schemes._gitrepo import BaseRepoBackend

    class _Recording(BaseRepoBackend):
        def __init__(self):
            self.params = []

        def request(self, method, url, params=None, **kwargs):
            self.params.append(params)
            response = requests.Response()
            response.status_code = 200
            response._content = b"branch content"
            response.headers["Content-Type"] = "application/octet-stream"
            return response

    backend = _Recording()
    p = GitHubPath(
        "github://github.com/acme/widgets/a.txt?ref=feat%26x", backend=backend
    )
    assert p.ref == "feat&x"
    assert p.read_bytes() == b"branch content"
    assert backend.params == [{"ref": "feat&x"}]
    assert p.as_uri() == "github://github.com/acme/widgets/a.txt?ref=feat%26x"


# --- gittools-open-rplus-silently-writable ---------------------------------


@pytest.mark.parametrize("mode", ["r+b", "w", "ab", "x"])
def test_github_write_modes_raise_not_implemented(github_api_server, mode):
    with pytest.raises(NotImplementedError):
        _github(github_api_server, "a.txt").open(mode)


@pytest.mark.parametrize("mode", ["r+b", "w", "ab", "x"])
def test_gitlab_write_modes_raise_not_implemented(gitlab_api_server, mode):
    with pytest.raises(NotImplementedError):
        _gitlab(gitlab_api_server, "a.txt").open(mode)


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_read_stream_is_read_only(github_api_server, gitlab_api_server, provider):
    import io

    if provider == "github":
        p = _github(github_api_server, "a.txt")
    else:
        p = _gitlab(gitlab_api_server, "a.txt")
    with p.open("rb") as f:
        assert not f.writable()
        with pytest.raises(io.UnsupportedOperation):
            f.write(b"patch")
