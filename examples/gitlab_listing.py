"""List files in a GitLab project using GitLabPath.
Requires the `http` extra (`pip install pathlib_next[http]`) -- plain
`requests`, no python-gitlab SDK. Works unauthenticated for public
projects; set GITLAB_TOKEN for a private one -- guarded under
`if __name__ == "__main__"` so importing this module is always safe, and
skipped (prints setup instructions, exit 0) unless GITLAB_EXAMPLE_PROJECT
is set.

Run directly:

    export GITLAB_EXAMPLE_PROJECT=gitlab-org/gitlab
    export GITLAB_EXAMPLE_PATH=doc         # optional, defaults to project root
    export GITLAB_EXAMPLE_REF=master       # optional, defaults to the default branch
    export GITLAB_EXAMPLE_HOST=gitlab.com  # optional, for a self-hosted instance
    export GITLAB_TOKEN=...                # optional
    python examples/gitlab_listing.py
"""
import os
import sys

from pathlib_next.uri.schemes.gitlab import GitLabPath, RepoBackend


def list_gitlab(host: str, owner_repo: str, path: str, ref: str, token: str):
    query = f"?ref={ref}" if ref else ""
    uri = f"gitlab://{host}/{owner_repo}/{path.lstrip('/')}{query}"
    backend = RepoBackend(token=token) if token else None
    root = GitLabPath(uri, backend=backend) if backend else GitLabPath(uri)
    print(f"Listing GitLab location: {uri}")
    for child in root.iterdir():
        kind = "dir " if child.is_dir() else "file"
        size = "" if child.is_dir() else f" ({child.stat().st_size} bytes)"
        print(f"  [{kind}] {child.name}{size}")


if __name__ == "__main__":
    owner_repo = os.environ.get("GITLAB_EXAMPLE_PROJECT")
    if not owner_repo:
        print(
            "GITLAB_EXAMPLE_PROJECT is not set -- skipping. See this file's "
            "module docstring for the required environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    host = os.environ.get("GITLAB_EXAMPLE_HOST", "gitlab.com")
    path = os.environ.get("GITLAB_EXAMPLE_PATH", "")
    ref = os.environ.get("GITLAB_EXAMPLE_REF", "")
    token = os.environ.get("GITLAB_TOKEN", "")

    try:
        list_gitlab(host, owner_repo, path, ref, token)
    except Exception as error:
        print(f"Could not list GitLab project {owner_repo} ({error}); skipping.", file=sys.stderr)
