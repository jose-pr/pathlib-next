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
import threading

import pytest

pytest.importorskip("requests")

from pathlib_next.uri.schemes.github import GitHubPath, RepoBackend
from pathlib_next.uri.schemes.git import GitHubGitPath, GitLabGitPath
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
    assert UriPath("git+github://ghe.internal/acme/widgets")._api_base == "https://ghe.internal/api/v3"
    assert UriPath("git+gitlab://gitlab.internal/acme/widgets")._api_base == "https://gitlab.internal/api/v4"


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


def test_github_symlink_entry_treated_as_file(fixture_tree):
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

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        backend = RepoBackend(api_base=f"http://127.0.0.1:{server.server_port}")
        p = GitHubPath("github://github.com/acme/widgets", backend=backend)
        entries = dict(p._scandir())
        assert not entries["link"].is_dir()
        assert entries["link"].st_size == 4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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


# --- error translation ----------------------------------------------------


@pytest.fixture
def status_server():
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

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
