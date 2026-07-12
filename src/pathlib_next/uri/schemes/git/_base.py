from __future__ import annotations

from ... import Uri, UriPath
from ..github import GitHubPath
from ..gitlab import GitLabPath


class GitPath(UriPath):
    """`git:` catch-all scheme for public Git hosts.

    `git://github.com/...` and `git://gitlab.com/...` auto-select the
    existing `github:`/`gitlab:` providers by host. Self-hosted or
    enterprise instances are intentionally ambiguous here and must use
    `git+github:`/`git+gitlab:` or the explicit `github:`/`gitlab:`
    schemes.
    """

    __SCHEMES = ("git",)
    __slots__ = ()

    @staticmethod
    def _normalize_host(host: str | None) -> str:
        return (host or "").lower()

    def __new__(cls, *args, **kwargs):
        uri = Uri(*args, **kwargs)
        host = cls._normalize_host(uri.source.host)
        if host in ("github.com", "www.github.com"):
            provider_cls = GitHubPath
        elif host in ("gitlab.com", "www.gitlab.com"):
            provider_cls = GitLabPath
        else:
            raise ValueError(
                f"git: can only auto-detect github.com and gitlab.com; "
                f"use github:, gitlab:, git+github:, or git+gitlab: for {uri!r}"
            )
        return UriPath.__new__(provider_cls, *args, **kwargs)

