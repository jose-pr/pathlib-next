"""The children of a `gitlab:` root look the default branch up once: they
share the backend, and so its cache, that the first of them builds."""

import pytest

pytest.importorskip("requests")

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes._gitrepo import RepoBackend
from pathlib_next.uri.schemes.gitlab import GitLabPath


class _CountingRepoBackend(RepoBackend):
    __slots__ = ("calls",)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return super().request(method, url, **kwargs)


class GitLabLoopbackG12Path(GitLabPath):
    """`gitlab:` whose derived backend talks to `api_base` instead."""

    __SCHEMES = ("gitlab-loopback",)
    __slots__ = ()
    api_base = None
    calls = []

    def _initbackend(self):
        backend = _CountingRepoBackend(api_base=type(self).api_base)
        backend.calls = type(self).calls
        return backend


def test_the_children_of_a_gitlab_root_look_the_default_branch_up_once(
    gitlab_api_server,
):
    base, owner, repo = gitlab_api_server
    GitLabLoopbackG12Path.api_base = base + "/api/v4"
    GitLabLoopbackG12Path.calls = calls = []
    root = UriPath(f"gitlab-loopback://x/{owner}/{repo}")
    assert (root / "a.txt").read_bytes() == b"a"
    assert (root / "b.py").read_bytes() == b"b"
    assert (root / "sub" / "c.py").read_bytes() == b"c"
    lookups = [
        url for _method, url in calls if url.endswith(f"/projects/{owner}%2F{repo}")
    ]
    assert len(lookups) == 1
