from __future__ import annotations

import base64 as _base64
import errno as _errno

from ... import utils as _utils
from ...utils.stat import FileStat
from ._gitrepo import (  # noqa: F401  (re-exported)
    InsecureTransportWarning,
    RepoBackend,
    _RepoApiPath,
    _translate_repo_errors,
)


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

    def _default_api_base(self) -> str:
        host = self.source.host or "github.com"
        if str(host).lower() in ("github.com", "www.github.com"):
            return "https://api.github.com"
        return f"https://{self._api_authority()}/api/v3"

    @property
    def _repo_url(self) -> str:
        owner, repo = self._api_quote(self.owner), self._api_quote(self.repo)
        return f"{self._api_base}/repos/{owner}/{repo}"

    def _contents_url(self, path: "str | None" = None) -> str:
        url = f"{self._repo_url}/contents"
        path = self.repo_path if path is None else path
        if path:
            url += f"/{self._api_quote(path, '/')}"
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

    def _object(self, data: dict) -> dict:
        """`data` if it describes one entry of a repository (it has a
        `type`), else `OSError(EIO)`."""
        if not isinstance(data.get("type"), str):
            raise self._bad_reply("an object without a type")
        return data

    def _entry(self, item) -> dict:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("type"), str)
        ):
            raise self._bad_reply("a directory entry without a name and a type")
        self._size_of(item)
        return item

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        data = self._decode(self._request(), list, dict)
        if isinstance(data, list):
            return FileStat(is_dir=True)
        return FileStat(st_size=self._size_of(self._object(data)), is_dir=False)

    def _entries(self, path: str) -> "list[dict]":
        """Contents-API-shaped entries of the directory `path`. A listing
        that hits the contents API's 1,000-entry cap is re-read through the
        Git Trees API, which has no such cap."""
        data = self._decode(self._request(url=self._contents_url(path)), list, dict)
        if isinstance(data, dict):
            self._object(data)
            raise NotADirectoryError(self)
        entries = [self._entry(item) for item in data]
        if len(entries) >= self._CONTENTS_LIMIT:
            entries = self._tree_entries(self._tree_sha(path))
        return entries

    def _tree_sha(self, path: str) -> str:
        if not path:
            return self.ref or self._default_branch()
        parent, _, name = path.rpartition("/")
        for entry in self._entries(parent):
            if entry["name"] == name and entry["type"] == "dir":
                sha = entry.get("sha")
                if not isinstance(sha, str):
                    raise self._bad_reply("a directory entry without a sha")
                return sha
        raise FileNotFoundError(self)

    def _default_branch(self) -> str:
        cache = self._backend_cache()
        key = ("github_default_branch", self._repo_url)
        if cache is not None and key in cache:
            return cache[key]
        data = self._decode(self._request(url=self._repo_url, params={}), dict)
        branch = data.get("default_branch")
        if not (isinstance(branch, str) and branch):
            raise self._bad_reply("a repository without a default_branch")
        if cache is not None:
            cache[key] = branch
        return branch

    def _tree_entries(self, sha: str) -> "list[dict]":
        url = f"{self._repo_url}/git/trees/{self._api_quote(sha, '/')}"
        data = self._decode(self._request(url=url, params={}), dict)
        if data.get("truncated"):
            # Only for trees far past any directory listing (100,000
            # entries); never return a partial listing as a complete one.
            raise OSError(_errno.EIO, f"Git tree listing truncated for {self}")
        tree = data.get("tree")
        if not isinstance(tree, list):
            raise self._bad_reply("a tree without a tree list")
        kinds = {"tree": "dir", "blob": "file", "commit": "submodule"}
        entries = []
        for item in tree:
            if not (
                isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("type"), str)
            ):
                raise self._bad_reply("a tree entry without a path and a type")
            entries.append(
                self._entry(
                    {
                        "name": item["path"],
                        "type": kinds.get(item["type"], item["type"]),
                        "size": item.get("size"),
                        "sha": item.get("sha"),
                    }
                )
            )
        return entries

    def _scandir(self):
        for entry in self._entries(self.repo_path):
            # The API's names are not trusted to be one path component.
            if not _utils.is_safe_child_name(entry["name"]):
                continue
            is_dir = entry["type"] == "dir"
            yield entry["name"], FileStat(
                st_size=0 if is_dir else self._size_of(entry), is_dir=is_dir
            )

    def _open(self, mode="r", buffering=-1):
        self._check_read_mode(mode)
        resp = self._request(headers={"Accept": self._RAW_ACCEPT})
        content_type = resp.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            # "raw" is ignored by the API for a directory listing (and for
            # a symlink/submodule entry, which carries its own JSON shape).
            data = self._decode(resp, list, dict)
            if isinstance(data, list):
                raise IsADirectoryError(self)
            content = data.get("content")
            if data.get("encoding") == "base64" and isinstance(content, str):
                try:
                    return self._reader(_base64.b64decode(content))
                except ValueError:
                    raise self._bad_reply("content that is not base64") from None
            raise OSError(_errno.EIO, f"Unsupported content response for {self}")
        return self._reader(resp.content)
