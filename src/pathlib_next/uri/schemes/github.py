from __future__ import annotations

import base64 as _base64
import errno as _errno
import io as _io
import urllib.parse as _urlparse

from ...utils.stat import FileStat
from ._gitrepo import RepoBackend, _RepoApiPath, _translate_repo_errors  # noqa: F401  (re-exported)


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
