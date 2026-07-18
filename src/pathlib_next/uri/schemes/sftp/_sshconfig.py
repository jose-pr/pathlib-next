"""Backend-agnostic SSH-config helpers (no paramiko/asyncssh import).

The default-config sentinel and path normalization live here, separate from
``_paramiko.py``, so the asyncssh backend and the scheme's ``__init__`` can
reference them **without importing paramiko**. Only the actual config *parsing*
(``_load_ssh_config``/``_lookup_ssh_config`` in ``_paramiko.py``) needs
``paramiko.SSHConfig``; the sentinel and the "which files" logic do not.
"""

from __future__ import annotations

import pathlib as _pathlib

#: Sentinel meaning "use the default SSH config location(s)". A bare ``object()``
#: so it is distinct from ``None`` (explicitly no config) and from any real path.
#: Shared by both backends; kept paramiko-free on purpose (see module docstring).
_DEFAULT_SSH_CONFIG = object()


def _normalize_config_paths(
    ssh_config: "object",
) -> "tuple[str, ...] | None":
    """Resolve an ``ssh_config`` argument to a tuple of file paths, or ``None``.

    ``_DEFAULT_SSH_CONFIG`` -> the user's ``~/.ssh/config``; ``None`` -> no config;
    a str/path -> that one file; an iterable -> those files. No paramiko needed.
    """
    if ssh_config is _DEFAULT_SSH_CONFIG:
        return (str(_pathlib.Path.home() / ".ssh" / "config"),)
    if ssh_config is None:
        return None
    if isinstance(ssh_config, (str, _pathlib.PurePath)):
        return (str(ssh_config),)
    return tuple(str(path) for path in ssh_config)
