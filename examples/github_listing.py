"""List files in a GitHub repository using GitHubPath.
Requires the `http` extra (`pip install pathlib_next[http]`) -- plain
`requests`, no PyGithub SDK. Works unauthenticated for public repos
(subject to GitHub's 60 req/h anonymous rate limit); set GITHUB_TOKEN for
a higher limit or a private repo -- guarded under `if __name__ == "__main__"`
so importing this module is always safe, and skipped (prints setup
instructions, exit 0) unless GITHUB_EXAMPLE_REPO is set.

Run directly:

    export GITHUB_EXAMPLE_REPO=psf/requests
    export GITHUB_EXAMPLE_PATH=requests   # optional, defaults to repo root
    export GITHUB_EXAMPLE_REF=main        # optional, defaults to the default branch
    export GITHUB_TOKEN=...               # optional
    python examples/github_listing.py
"""
import os
import sys

from pathlib_next.uri.schemes.github import GitHubPath, RepoBackend


def list_github(owner_repo: str, path: str, ref: str, token: str):
    query = f"?ref={ref}" if ref else ""
    uri = f"github://github.com/{owner_repo}/{path.lstrip('/')}{query}"
    backend = RepoBackend(token=token) if token else None
    root = GitHubPath(uri, backend=backend) if backend else GitHubPath(uri)
    print(f"Listing GitHub location: {uri}")
    for child in root.iterdir():
        kind = "dir " if child.is_dir() else "file"
        size = "" if child.is_dir() else f" ({child.stat().st_size} bytes)"
        print(f"  [{kind}] {child.name}{size}")


if __name__ == "__main__":
    owner_repo = os.environ.get("GITHUB_EXAMPLE_REPO")
    if not owner_repo:
        print(
            "GITHUB_EXAMPLE_REPO is not set -- skipping. See this file's "
            "module docstring for the required environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    path = os.environ.get("GITHUB_EXAMPLE_PATH", "")
    ref = os.environ.get("GITHUB_EXAMPLE_REF", "")
    token = os.environ.get("GITHUB_TOKEN", "")

    try:
        list_github(owner_repo, path, ref, token)
    except Exception as error:
        print(f"Could not list GitHub repo {owner_repo} ({error}); skipping.", file=sys.stderr)
