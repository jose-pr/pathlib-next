from __future__ import annotations

import io as _io
import urllib.parse as _urlparse

from ...utils.stat import FileStat
from ._gitrepo import RepoBackend, _RepoApiPath, _translate_repo_errors  # noqa: F401  (re-exported)


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
