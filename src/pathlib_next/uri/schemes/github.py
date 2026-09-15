from __future__ import annotations

import base64 as _base64
import errno as _errno
import urllib.parse as _urlparse

from ...utils.stat import FileStat
from ._gitrepo import (
    RepoBackend,
    _RepoApiPath,
    _translate_repo_errors,
)  # noqa: F401  (re-exported)


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
    # The contents API returns at most this many entries per directory, with
    # no pagination and no truncation flag.
    _CONTENTS_LIMIT = 1000

    @property
    def _api_base(self) -> str:
        override = getattr(self.backend, "api_base", None)
        if override:
            return override
        host = self.source.host or "github.com"
        if str(host).lower() in ("github.com", "www.github.com"):
            return "https://api.github.com"
        return f"https://{self._api_authority()}/api/v3"

    @property
    def _repo_url(self) -> str:
        return f"{self._api_base}/repos/{self.owner}/{self.repo}"

    def _contents_url(self, path: "str | None" = None) -> str:
        url = f"{self._repo_url}/contents"
        path = self.repo_path if path is None else path
        if path:
            url += f"/{_urlparse.quote(path)}"
        return url

    def _params(self) -> dict:
        ref = self.ref
        return {"ref": ref} if ref else {}

    def _request(self, headers=None, url=None, params=None):
        with _translate_repo_errors(self):
            resp = self.backend.request(
                "GET",
                url or self._contents_url(),
                params=self._params() if params is None else params,
                headers=headers,
            )
            # A rate-limited reply (403/429) becomes EAGAIN in
            # `_translate_repo_errors`.
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

    def _entries(self, path: str) -> "list[dict]":
        """Contents-API-shaped entries of the directory `path`. A listing
        that hits the contents API's 1,000-entry cap is re-read through the
        Git Trees API, which has no such cap."""
        data = self._request(url=self._contents_url(path)).json()
        if not isinstance(data, list):
            raise NotADirectoryError(self)
        if len(data) >= self._CONTENTS_LIMIT:
            data = self._tree_entries(self._tree_sha(path))
        return data

    def _tree_sha(self, path: str) -> str:
        if not path:
            return self.ref or self._default_branch()
        parent, _, name = path.rpartition("/")
        for entry in self._entries(parent):
            if entry["name"] == name and entry["type"] == "dir":
                return entry["sha"]
        raise FileNotFoundError(self)

    def _default_branch(self) -> str:
        cache = self._backend_cache()
        key = ("github_default_branch", self._repo_url)
        if cache is not None and key in cache:
            return cache[key]
        branch = self._request(url=self._repo_url, params={}).json()["default_branch"]
        if cache is not None:
            cache[key] = branch
        return branch

    def _tree_entries(self, sha: str) -> "list[dict]":
        url = f"{self._repo_url}/git/trees/{_urlparse.quote(sha)}"
        data = self._request(url=url, params={}).json()
        if data.get("truncated"):
            # Only for trees far past any directory listing (100,000
            # entries); never return a partial listing as a complete one.
            raise OSError(_errno.EIO, f"Git tree listing truncated for {self}")
        kinds = {"tree": "dir", "blob": "file", "commit": "submodule"}
        return [
            {
                "name": entry["path"],
                "type": kinds.get(entry["type"], entry["type"]),
                "size": entry.get("size", 0) or 0,
                "sha": entry.get("sha"),
            }
            for entry in data.get("tree", [])
        ]

    def _scandir(self):
        for entry in self._entries(self.repo_path):
            is_dir = entry["type"] == "dir"
            yield entry["name"], FileStat(
                st_size=0 if is_dir else (entry.get("size", 0) or 0), is_dir=is_dir
            )

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def _open(self, mode="r", buffering=-1):
        self._check_read_mode(mode)
        resp = self._request(headers={"Accept": self._RAW_ACCEPT})
        content_type = resp.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            # "raw" is ignored by the API for a directory listing (and for
            # a symlink/submodule entry, which carries its own JSON shape).
            data = resp.json()
            if isinstance(data, list):
                raise IsADirectoryError(self)
            if data.get("encoding") == "base64" and data.get("content") is not None:
                return self._reader(_base64.b64decode(data["content"]))
            raise OSError(_errno.EIO, f"Unsupported content response for {self}")
        return self._reader(resp.content)
