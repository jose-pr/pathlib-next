from __future__ import annotations

import errno as _errno
import io as _io
import itertools as _itertools
import urllib.parse as _urlparse

from ...utils.stat import FileStat
from ._gitrepo import (
    RepoBackend,
    _RepoApiPath,
    _translate_repo_errors,
)  # noqa: F401  (re-exported)


class GitLabPath(_RepoApiPath):
    """`gitlab:` scheme: read-only access to a GitLab project's repository
    tree via the REST API v4 (project identified as URL-encoded
    `owner/repo`). A project in a subgroup is addressed with GitLab's own
    `/-/` separator, as in its web URLs:
    `gitlab://host/group/subgroup/project/-/path/in/repo`; without the
    separator the first two segments are owner/repo. `host` defaults to
    `gitlab.com`; a self-hosted instance is just a different host (and
    port), always at `https://{host}/api/v4` (no enterprise/SaaS API-path
    split like GitHub). The tree-listing endpoint
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
        if not self.source.host:
            return "https://gitlab.com/api/v4"
        return f"https://{self._api_authority()}/api/v4"

    def _separator_index(self) -> "int | None":
        # GitLab's `/-/` ends the project path; it needs at least
        # `owner/repo` in front of it.
        try:
            return self.segments.index("-", 3)
        except ValueError:
            return None

    @property
    def owner(self) -> str:
        index = self._separator_index()
        if index is None:
            return super().owner
        return "/".join(self.segments[1 : index - 1])

    @property
    def repo(self) -> str:
        index = self._separator_index()
        if index is None:
            return super().repo
        return self.segments[index - 1]

    @property
    def repo_path(self) -> str:
        index = self._separator_index()
        if index is None:
            return super().repo_path
        return "/".join(self.segments[index + 1 :]).rstrip("/")

    def _make_child_relpath(self, name: str, **kwargs):
        parent = self
        if name == "-" and self._separator_index() is None and len(self.segments) > 2:
            # A directory literally named "-" would read as the separator:
            # spell the parent with `/-/` so the child stays that directory.
            segments = self.segments[:3] + ("-",) + self.segments[3:]
            parent = self._from_parsed_parts(
                self.source, "/".join(segments), self.query, self.fragment
            )
        return super(GitLabPath, parent)._make_child_relpath(name, **kwargs)

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
        resp = self._request("GET", f"{self._api_base}/projects/{self._project_id}")
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
        # The tree endpoint is paginated (at most 100 per page): follow
        # `X-Next-Page` until the last page.
        page = 1
        while True:
            resp = self._request(
                "GET",
                self._tree_url(),
                params=self._params(path=path, per_page=100, page=page),
            )
            yield from resp.json()
            next_page = resp.headers.get("X-Next-Page", "")
            if not next_page.isdigit() or int(next_page) <= page:
                return
            page = int(next_page)

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        path = self.repo_path
        if not path:
            # Ask the server: a missing project, owner-only URI or unknown
            # ref must not read as an existing directory.
            self._request("GET", self._tree_url(), params=self._params(per_page=1))
            return FileStat(is_dir=True)
        meta = self._get_file_meta(path)
        if meta is not None:
            return FileStat(st_size=meta.get("size", 0) or 0, is_dir=False)
        # Not a file at this exact path. Git has no empty directories, so a
        # path whose tree listing has any entry is a directory; an empty
        # listing (or a 404) means it does not exist.
        try:
            resp = self._request(
                "GET", self._tree_url(), params=self._params(path=path, per_page=1)
            )
        except FileNotFoundError:
            raise FileNotFoundError(self) from None
        if resp.json():
            return FileStat(is_dir=True)
        raise FileNotFoundError(self)

    def _scandir(self):
        entries = self._tree_entries(self.repo_path)
        try:
            first = next(entries, None)
        except FileNotFoundError:
            # The tree endpoint 404s for a blob path as for a missing one.
            if self.repo_path and self._get_file_meta(self.repo_path) is not None:
                raise NotADirectoryError(
                    _errno.ENOTDIR, "Not a directory", str(self)
                ) from None
            raise
        if first is None:
            return
        for entry in _itertools.chain((first,), entries):
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
        try:
            resp = self._request(
                "GET",
                self._file_url(path, "/raw"),
                params={"ref": self._resolved_ref()},
            )
        except FileNotFoundError:
            # The files endpoint 404s for a tree path as for a missing one.
            try:
                is_dir = self.stat().is_dir()
            except FileNotFoundError:
                is_dir = False
            if is_dir:
                raise IsADirectoryError(
                    _errno.EISDIR, "Is a directory", str(self)
                ) from None
            raise
        return _io.BytesIO(resp.content)
