from ..gitlab import GitLabPath


class GitLabGitPath(GitLabPath):
    """`git+gitlab:` explicit GitLab-hosted repository scheme."""

    __SCHEMES = ("git+gitlab",)
    __slots__ = ()

