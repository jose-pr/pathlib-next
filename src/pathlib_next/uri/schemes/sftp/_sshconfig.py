"""Backend-agnostic SSH-config helpers (no paramiko/asyncssh import).

The default-config sentinel and path normalization live here, separate from
``_paramiko.py``, so the asyncssh backend and the scheme's ``__init__`` can
reference them **without importing paramiko**. Only the actual config *parsing*
(``_load_ssh_config``/``_lookup_ssh_config`` in ``_paramiko.py``) needs
``paramiko.SSHConfig``; the sentinel and the "which files" logic do not.
"""

from __future__ import annotations

import glob as _glob
import pathlib as _pathlib
import re as _re

#: Sentinel meaning "use the default SSH config location(s)". A bare ``object()``
#: so it is distinct from ``None`` (explicitly no config) and from any real path.
#: Shared by both backends; kept paramiko-free on purpose (see module docstring).
_DEFAULT_SSH_CONFIG = object()

# OpenSSH's own limit (readconf.c READCONF_MAX_DEPTH).
_INCLUDE_MAX_DEPTH = 16
_DIRECTIVE = _re.compile(r"^\s*(\w+)(?:\s*=\s*|\s+)(.*?)\s*$")


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


def _expand_includes(path: "str | _pathlib.Path", _depth: int = 0) -> "list[str]":
    """The lines of the ssh_config file ``path``, with every ``Include``
    directive replaced by the lines of the files it names.

    paramiko's ``SSHConfig.parse`` has no ``Include`` support and would
    silently store it as an unknown key. Resolution follows asyncssh (the
    other backend) and OpenSSH: ``~`` is expanded, a relative pattern is
    taken relative to ``~/.ssh``, globs expand in sorted order, and a pattern
    matching no file is skipped. After an included file, the enclosing
    ``Host``/``Match`` header is re-emitted so the including file's following
    lines keep the block they were written in (OpenSSH restores that state
    too).
    """
    if _depth > _INCLUDE_MAX_DEPTH:
        raise ValueError(f"ssh_config Include nesting deeper than {_INCLUDE_MAX_DEPTH}")
    lines: "list[str]" = []
    header = "Host *"
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = _DIRECTIVE.match(line)
            keyword = match.group(1).lower() if match else ""
            if keyword in ("host", "match"):
                header = line.strip()
            if keyword != "include":
                lines.append(line)
                continue
            for pattern in match.group(2).split():
                pattern = _pathlib.Path(pattern.strip('"')).expanduser()
                if not pattern.anchor:
                    pattern = _pathlib.Path.home() / ".ssh" / pattern
                for included in sorted(_glob.glob(str(pattern))):
                    if _pathlib.Path(included).is_file():
                        lines.extend(_expand_includes(included, _depth + 1))
            lines.append(header + "\n")
    return lines
