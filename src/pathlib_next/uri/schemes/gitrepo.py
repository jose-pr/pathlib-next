from __future__ import annotations

import base64 as _base64
import contextlib as _contextlib
import errno as _errno
import io as _io
import typing as _ty
import urllib.parse as _urlparse

import requests as _req

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import UriPath


@_contextlib.contextmanager
def _translate_repo_errors(path_obj):
    try:
        yield
    except _req.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 404:
            raise FileNotFoundError(path_obj) from e
        elif status in (401, 403):
            raise PermissionError(path_obj) from e
        else:
            raise OSError(_errno.EIO, f"HTTP Error {status} for {path_obj}") from e
    except _req.exceptions.Timeout as e:
        raise TimeoutError(f"Timeout for {path_obj}") from e
    except _req.exceptions.ConnectionError as e:
        raise ConnectionError(f"Connection error for {path_obj}") from e
    except _req.exceptions.RequestException as e:
        raise OSError(_errno.EIO, f"Request failed for {path_obj}") from e


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
    real convention always forces `https`."""

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
        headers = dict(headers or {})
        if self.token:
            headers.setdefault("Authorization", f"Bearer {self.token}")
        return self.session.request(
            method, url, headers=headers, **{**self.requests_args, **kwargs}
        )


class _RepoApiPath(UriPath):
    """Shared base for `github:`/`gitlab:` (`<scheme>://host/owner/repo/path
    /in/repo?ref=<ref>`): read-only access to a git-hosting REST API. `ref`
    (branch/tag/SHA) is always optional, carried in the `?ref=` query string
    -- omitted, both APIs fall back server-side to the repo's default
    branch, so no client-side lookup is needed. `host` defaults to the
    public SaaS host; any other host is treated as a self-hosted instance.
    Auth: a bearer token via `with_backend(RepoBackend(token=...))`, or
    embedded in the URI as userinfo (`<scheme>://TOKEN@host/...`, like
    `ftp:`/`sftp:` embed credentials) -- the backend kwarg wins if both are
    given. Read-only: write goes through a separate commits API entirely
    (out of scope, see `docs/divergences.md`)."""

    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseRepoBackend

    def _initbackend(self):
        token = self.source.parsed_userinfo()[0] or None
        return RepoBackend(token=token)

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
        return "/".join(self.segments[3:])

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


class GitHubPath(_RepoApiPath):
    """`github:` scheme: read-only access to a GitHub repository tree via
    the REST contents API (`GET /repos/{owner}/{repo}/contents/{path}`).
    `host` defaults to `github.com` (API at `api.github.com`); any other
    host is treated as GitHub Enterprise (API at `https://{host}/api/v3`).
    File bodies are fetched with the `raw` media type (skips base64 and its
    ~1MB inline-content cap) instead of the default JSON+base64 envelope.
    `symlink`/`submodule` tree entries are treated as plain files (no
    special handling -- see `docs/divergences.md`). No mtime (would need a
    separate commits-history call per path). Requires the `http` extra
    (plain `requests`, no PyGithub SDK)."""

    __SCHEMES = ("github",)
    __slots__ = ()

    _RAW_ACCEPT = "application/vnd.github.raw+json"

    @property
    def _api_base(self) -> str:
        override = getattr(self.backend, "api_base", None)
        if override:
            return override
        host = self.source.host or "github.com"
        if host in ("github.com", "www.github.com"):
            return "https://api.github.com"
        return f"https://{host}/api/v3"

    def _contents_url(self) -> str:
        url = f"{self._api_base}/repos/{self.owner}/{self.repo}/contents"
        path = self.repo_path
        if path:
            url += f"/{_urlparse.quote(path)}"
        return url

    def _params(self) -> dict:
        ref = self.ref
        return {"ref": ref} if ref else {}

    def _request(self, headers=None):
        with _translate_repo_errors(self):
            resp = self.backend.request(
                "GET", self._contents_url(), params=self._params(), headers=headers
            )
            if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
                reset = resp.headers.get("X-RateLimit-Reset", "?")
                raise OSError(
                    _errno.EAGAIN,
                    f"GitHub API rate limit exceeded for {self} (resets at {reset})",
                )
            resp.raise_for_status()
        return resp

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        data = self._request().json()
        if isinstance(data, list):
            return FileStat(is_dir=True)
        return FileStat(st_size=data.get("size", 0) or 0, is_dir=False)

    def _scandir(self):
        data = self._request().json()
        if not isinstance(data, list):
            raise NotADirectoryError(self)
        for entry in data:
            is_dir = entry["type"] == "dir"
            yield entry["name"], FileStat(
                st_size=0 if is_dir else (entry.get("size", 0) or 0), is_dir=is_dir
            )

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def _open(self, mode="r", buffering=-1):
        if "r" not in mode:
            raise NotImplementedError(f"open(mode={mode!r})")
        resp = self._request(headers={"Accept": self._RAW_ACCEPT})
        content_type = resp.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            # "raw" is ignored by the API for a directory listing (and for
            # a symlink/submodule entry, which carries its own JSON shape).
            data = resp.json()
            if isinstance(data, list):
                raise IsADirectoryError(self)
            if data.get("encoding") == "base64" and data.get("content") is not None:
                return _io.BytesIO(_base64.b64decode(data["content"]))
            raise OSError(_errno.EIO, f"Unsupported content response for {self}")
        return _io.BytesIO(resp.content)


class GitLabPath(_RepoApiPath):
    """`gitlab:` scheme: read-only access to a GitLab project's repository
    tree via the REST API v4 (project identified as URL-encoded
    `owner/repo`). `host` defaults to `gitlab.com`; a self-hosted instance
    is just a different host, always at `https://{host}/api/v4` (no
    enterprise/SaaS API-path split like GitHub). The tree-listing endpoint
    doesn't carry file size, so `_scandir()` only pre-seeds a stat hint for
    `tree` (directory) entries (`size=0` is truthful for a directory, not a
    placeholder) -- `blob` (file) entries get a real `stat()` lazily
    instead of guessing a size that could poison a caller trusting the
    hint. No mtime. Requires the `http` extra (plain `requests`, no
    python-gitlab SDK)."""

    __SCHEMES = ("gitlab",)
    __slots__ = ()

    @property
    def _api_base(self) -> str:
        override = getattr(self.backend, "api_base", None)
        if override:
            return override
        host = self.source.host or "gitlab.com"
        return f"https://{host}/api/v4"

    @property
    def _project_id(self) -> str:
        return _urlparse.quote(f"{self.owner}/{self.repo}", safe="")

    def _params(self, **extra) -> dict:
        ref = self.ref
        if ref:
            extra["ref"] = ref
        return extra

    def _file_url(self, path: str, suffix: str = "") -> str:
        encoded = _urlparse.quote(path, safe="")
        return f"{self._api_base}/projects/{self._project_id}/repository/files/{encoded}{suffix}"

    def _tree_url(self) -> str:
        return f"{self._api_base}/projects/{self._project_id}/repository/tree"

    def _request(self, method, url, **kwargs):
        with _translate_repo_errors(self):
            resp = self.backend.request(method, url, **kwargs)
            resp.raise_for_status()
        return resp

    def _resolved_ref(self) -> str:
        # Unlike the tree endpoint (ref optional, defaults server-side),
        # GitLab's repository/files endpoints (metadata AND raw) 400 with
        # "ref is missing, ref is empty" if `ref` is omitted entirely --
        # confirmed live against gitlab.com, not documented clearly. Resolve
        # and cache the project's default branch once per backend instead.
        ref = self.ref
        if ref:
            return ref
        cache = self.backend.cache
        key = ("gitlab_default_branch", self._api_base, self._project_id)
        if key in cache:
            return cache[key]
        resp = self._request(
            "GET", f"{self._api_base}/projects/{self._project_id}"
        )
        branch = resp.json()["default_branch"]
        cache[key] = branch
        return branch

    def _get_file_meta(self, path: str):
        try:
            resp = self._request(
                "GET", self._file_url(path), params={"ref": self._resolved_ref()}
            )
        except FileNotFoundError:
            return None
        return resp.json()

    def _tree_entries(self, path: str):
        resp = self._request(
            "GET", self._tree_url(), params=self._params(path=path, per_page=100)
        )
        return resp.json()

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        path = self.repo_path
        if not path:
            return FileStat(is_dir=True)
        meta = self._get_file_meta(path)
        if meta is not None:
            return FileStat(st_size=meta.get("size", 0) or 0, is_dir=False)
        # Not a file at this exact path -- the tree-listing endpoint alone
        # can't tell "empty directory" from "doesn't exist" (both are `[]`),
        # so check the PARENT's listing for a same-named entry instead.
        parent, _, name = path.rpartition("/")
        for entry in self._tree_entries(parent):
            if entry["name"] == name:
                return FileStat(is_dir=entry["type"] == "tree")
        raise FileNotFoundError(self)

    def _scandir(self):
        for entry in self._tree_entries(self.repo_path):
            is_dir = entry["type"] == "tree"
            yield entry["name"], (FileStat(is_dir=True) if is_dir else None)

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def _open(self, mode="r", buffering=-1):
        if "r" not in mode:
            raise NotImplementedError(f"open(mode={mode!r})")
        path = self.repo_path
        if not path:
            raise IsADirectoryError(self)
        resp = self._request(
            "GET", self._file_url(path, "/raw"), params={"ref": self._resolved_ref()}
        )
        return _io.BytesIO(resp.content)
