from ..github import GitHubPath


class GitHubGitPath(GitHubPath):
    """`git+github:` explicit GitHub-hosted repository scheme."""

    __SCHEMES = ("git+github",)
    __slots__ = ()

