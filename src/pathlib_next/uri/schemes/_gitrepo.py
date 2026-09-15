from __future__ import annotations

import contextlib as _contextlib
import errno as _errno
import typing as _ty

import requests as _req

from ... import utils as _utils
from .. import Source, UriPath
from ..source import _compose_host

DEFAULT_TIMEOUT = (10, 60)
"""`(connect, read)` timeout, in seconds, `RepoBackend` sends with every
request unless the caller supplies `timeout` (`RepoBackend(timeout=...)` or
per request); `timeout=None` restores requests' unbounded wait."""


@_contextlib.contextmanager
def _translate_repo_errors(path_obj):
    # `from None`: never chain a requests exception, whose text can carry a
    # URL with credentials (an environment proxy URL included) into every
    # formatted traceback. The status code or exception type stays in the
    # message instead.
    try:
        yield
    except _req.exceptions.HTTPError as e:
        response = e.response
        status = response.status_code if response is not None else None
        reason = getattr(response, "reason", None) or ""
        if status == 404:
            raise FileNotFoundError(path_obj) from None
        elif status in (401, 403):
            raise PermissionError(path_obj) from None
        else:
            message = f"HTTP Error {status} {reason}".rstrip()
            raise OSError(_errno.EIO, f"{message} for {path_obj}") from None
    except _req.exceptions.Timeout as e:
        raise TimeoutError(f"Timeout for {path_obj} ({type(e).__name__})") from None
    except _req.exceptions.ConnectionError as e:
        raise ConnectionError(
            f"Connection error for {path_obj} ({type(e).__name__})"
        ) from None
    except _req.exceptions.RequestException as e:
        raise OSError(
            _errno.EIO, f"Request failed for {path_obj} ({type(e).__name__})"
        ) from None


class BaseRepoBackend(object):
    """Protocol for issuing an authenticated HTTP request against a
    GitHub/GitLab REST API. Subclass this to plug in custom auth/session
    handling; `RepoBackend` is the real `requests`-based implementation."""

    __slots__ = ()

    @_utils.notimplemented
    def request(self, method, url, **kwargs): ...


class RepoBackend(BaseRepoBackend):
    """Lazily-used `requests.Session` + an optional bearer `token` sent on
    every request (mirrors `HttpBackend.requests_args` in `http.py`).
    Unauthenticated works fine for public repos, subject to the host's
    anonymous rate limit (60 req/h for github.com). `api_base`, when set,
    overrides the scheme's convention-derived API root entirely (e.g.
    `https://api.github.com`/`https://{host}/api/v3`) -- the seam a test
    fake (or a reverse-proxied self-hosted setup) plugs into, since the
    real convention always forces `https`. Every request gets
    `timeout=DEFAULT_TIMEOUT` (`(10, 60)` seconds) unless `requests_args`
    (`RepoBackend(timeout=...)`) or the call supplies one; `timeout=None`
    waits forever."""

    __slots__ = ("session", "token", "api_base", "requests_args", "cache")

    def __init__(
        self,
        token: str = None,
        session: _req.Session = None,
        api_base: str = None,
        **requests_args,
    ):
        self.session = session or _req.Session()
        self.token = token or None
        self.api_base = api_base.rstrip("/") if api_base else None
        self.requests_args = requests_args
        # Generic per-backend memoization slot for schemes that need it
        # (GitLabPath's resolved default-branch lookup, see `_resolved_ref`)
        # -- shared across every path instance that shares this backend.
        self.cache = {}

    def request(self, method, url, headers=None, **kwargs):
        # `RepoBackend(headers=...)` merged key by key under the request's
        # own headers, instead of colliding with the `headers=` keyword.
        headers = {**(self.requests_args.get("headers") or {}), **(headers or {})}
        if self.token:
            headers.setdefault("Authorization", f"Bearer {self.token}")
        args = {**self.requests_args, **kwargs}
        args.pop("headers", None)
        args.setdefault("timeout", DEFAULT_TIMEOUT)
        return self.session.request(method, url, headers=headers, **args)


class _RepoApiPath(UriPath):
    """Shared base for `github:`/`gitlab:` (`<scheme>://host/owner/repo/path
    /in/repo?ref=<ref>`): read-only access to a git-hosting REST API. `ref`
    (branch/tag/SHA) is always optional, carried in the `?ref=` query string
    -- omitted, both APIs fall back server-side to the repo's default
    branch, so no client-side lookup is needed. `host` defaults to the
    public SaaS host; any other host is treated as a self-hosted instance.
    Auth: a bearer token via `with_backend(RepoBackend(token=...))`, or
    embedded in the URI as userinfo -- `<scheme>://user:TOKEN@host/...`
    (git's `x-access-token:TOKEN` / GitLab's `oauth2:TOKEN` convention: the
    password slot is the token) or a bare `<scheme>://TOKEN@host/...` --
    the backend kwarg wins if both are given. Because the user slot can be
    the secret here, `str()`, `repr()`, `as_uri(sanitize=True)` and so every
    exception message drop the WHOLE userinfo for these schemes (other
    schemes drop only the password); `as_uri()` still round-trips it.
    Read-only: write goes through a separate commits API entirely (out of
    scope, see `docs/divergences.md`)."""

    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseRepoBackend

    @classmethod
    def _format_parsed_parts(
        cls,
        source: Source,
        path: str,
        query: str,
        fragment: str,
        /,
        sanitize=True,
    ) -> str:
        # A bare `TOKEN@host` puts the secret in the user slot, which the
        # base sanitizing keeps -- drop the whole userinfo instead.
        if sanitize and source.userinfo:
            source = source._replace(userinfo=None)
        return super()._format_parsed_parts(
            source, path, query, fragment, sanitize=sanitize
        )

    def _initbackend(self):
        user, password = self.source.parsed_userinfo()
        return RepoBackend(token=password or user or None)

    @property
    def owner(self) -> str:
        segments = self.segments
        return segments[1] if len(segments) > 1 else ""

    @property
    def repo(self) -> str:
        segments = self.segments
        return segments[2] if len(segments) > 2 else ""

    @property
    def repo_path(self) -> str:
        # A trailing "/" (`.../docs/`) names the same directory.
        return "/".join(self.segments[3:]).rstrip("/")

    def _api_authority(self) -> str:
        """`host[:port]` of this URI for a self-hosted API root, with an
        IPv6 literal bracketed."""
        authority = _compose_host(self.source.host)
        if self.source.port:
            authority += f":{self.source.port}"
        return authority

    @property
    def ref(self) -> "str | None":
        from ..query import Query

        return Query(self.query or "").to_dict(single=True).get("ref") or None

    def _make_child_relpath(self, name: str, **kwargs) -> _ty.Self:
        # The base implementation always resets query/fragment to "" for a
        # child (uri/__init__.py's Uri._make_child_relpath) -- fine for
        # every other scheme (none of them give the query string semantic
        # meaning), but here it would silently drop `?ref=` on every
        # iterdir()/glob()/walk() descendant, falling back to the default
        # branch instead of the ref the caller asked for.
        inst = super()._make_child_relpath(name, **kwargs)
        if self.query:
            inst._query = self.query
        return inst
