"""Backend-agnostic SSH-config helpers (no paramiko/asyncssh import).

The default-config sentinel and path normalization live here, separate from
``_paramiko.py``, so the asyncssh backend and the scheme's ``__init__`` can
reference them **without importing paramiko**. Only the actual config *parsing*
(``_load_ssh_config``/``_lookup_ssh_config`` in ``_paramiko.py``) needs
``paramiko.SSHConfig``; the sentinel and the "which files" logic do not.
"""

from __future__ import annotations

import functools as _functools
import getpass as _getpass
import glob as _glob
import ipaddress as _ipaddress
import pathlib as _pathlib
import re as _re
import shlex as _shlex
import unicodedata as _unicodedata

#: Sentinel meaning "use the default SSH config location(s)". A bare ``object()``
#: so it is distinct from ``None`` (explicitly no config) and from any real path.
#: Shared by both backends; kept paramiko-free on purpose (see module docstring).
_DEFAULT_SSH_CONFIG = object()

# OpenSSH's own limit (readconf.c READCONF_MAX_DEPTH).
_INCLUDE_MAX_DEPTH = 16
_DIRECTIVE = _re.compile(r"^\s*(\w+)(?:\s*=\s*|\s+)(.*?)\s*$")

# An ASCII host name: letters, digits, "." "_" "-", not starting with "-".
_PLAIN_HOST = _re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*")
_PLAIN_CHAR = _re.compile(r"[A-Za-z0-9_.-]")
# The zone of a scoped IPv6 address ("fe80::1%eth0").
_ZONE = _re.compile(r"[A-Za-z0-9_.-]+")
# What OpenSSH refuses in a user name that is expanded into a command
# (valid_ruser(), CVE-2023-51385), with white space and control characters:
# a ProxyCommand is split on white space and quotes after the expansion.
_UNSAFE_USER = _re.compile(r"[\s\x00-\x1f\x7f'\"`$\\;&<>|(){}]|^-")
# A condition no host satisfies in an ssh_config ``Match`` (``!*`` negates a
# match of every name).
_NEVER = "originalhost '!*'"


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


@_functools.lru_cache(maxsize=256)
def _check_host(host: "object") -> None:
    """Raise ``ValueError`` unless ``host`` is a plain host name or address.

    Accepted: an IP address (an IPv6 one may carry a ``%zone``), and a name of
    ASCII letters, digits, ``.``, ``_`` and ``-`` (so an underscore and a
    trailing dot pass) that does not start with ``-``, plus the letters,
    marks and digits of other scripts (an IDN). Everything else is refused:
    white space, control characters, quotes, a leading ``-`` and every shell
    metacharacter. A host is untrusted input (a URI names it) and an ssh_config
    ``ProxyCommand`` or ``ProxyJump`` splits it into a command line.

    No host (``None`` or empty) is not checked: nothing is looked up for it.
    """
    if host is None or host == "":
        return
    text = str(host)
    if isinstance(host, _ipaddress.IPv6Address) or ":" in text:
        address, percent, zone = text.partition("%")
        try:
            _ipaddress.IPv6Address(address)
        except ValueError:
            pass
        else:
            if not percent or _ZONE.fullmatch(zone):
                return
    elif _PLAIN_HOST.fullmatch(text) or (not text.isascii() and _is_idn(text)):
        return
    raise ValueError(f"refusing SFTP host {text!r}: not a plain host name or address")


def _is_idn(text: str) -> bool:
    """Whether ``text`` is a host name of ASCII name characters and the
    letters, marks and decimal digits of other scripts, not starting with
    ``-``."""
    return text[0] != "-" and all(
        _PLAIN_CHAR.fullmatch(char)
        or _unicodedata.category(char)[0] in "LM"
        or _unicodedata.category(char) == "Nd"
        for char in text
    )


def _check_proxy_user(user: str) -> None:
    if _UNSAFE_USER.search(user):
        raise ValueError(
            f"refusing SFTP user {user!r} for a ProxyCommand: it holds white "
            "space, a quote, a shell metacharacter or starts with '-'"
        )


def _expand_proxy_command(
    command: str, *, host: str, hostname: str, port: int, user: "str | None"
) -> str:
    """``command`` with ssh_config's tokens expanded for one connection, in a
    single pass: ``%h`` is ``hostname`` (the ``HostName`` in effect), ``%n`` the
    ``host`` as given, ``%p`` the ``port`` and ``%r`` the ``user`` this
    connection uses (the local user when there is none), ``%%`` a percent
    sign. Another ``%x`` stays as written. A ``user`` that is not safe in a
    command line is refused when ``%r`` would put it there.
    """

    def replace(match: "_re.Match[str]") -> str:
        token = match.group(1)
        if token == "%":
            return "%"
        if token == "h":
            return hostname
        if token == "n":
            return host
        if token == "p":
            return str(port)
        if token == "r":
            name = user or _local_user()
            _check_proxy_user(name)
            return name
        return match.group(0)

    return _re.sub(r"%(.)", replace, command, flags=_re.DOTALL)


def _local_user() -> str:
    try:
        return _getpass.getuser()
    except Exception:
        return ""


def _host_criterion(patterns_text: str) -> "tuple[str, ...]":
    """The condition ``Host <patterns>`` states, as ``Match`` criteria: none
    for ``*`` alone, one ``originalhost`` list otherwise, a condition that
    never holds for what a list cannot express."""
    try:
        patterns = _shlex.split(patterns_text)
    except ValueError:
        return (_NEVER,)
    if patterns == ["*"]:
        return ()
    if any("," in pattern for pattern in patterns):
        return (_NEVER,)
    return (f"originalhost {_shlex.quote(','.join(patterns))}",)


def _match_criterion(criteria_text: str) -> "tuple[str, ...]":
    """The criteria of a ``Match`` line, without ``all`` (always true, and
    paramiko refuses it next to any other criterion)."""
    try:
        tokens = _shlex.split(criteria_text)
    except ValueError:
        return (_NEVER,)
    tokens = [token for token in tokens if token.lower() != "all"]
    return (_shlex.join(tokens),) if tokens else ()


def _scope_header(scope: "tuple[str, ...]") -> str:
    return "Match " + " ".join(scope) if scope else "Host *"


def _expand_includes(
    path: "str | _pathlib.Path",
    _depth: int = 0,
    _scope: "tuple[str, ...]" = (),
) -> "list[str]":
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

    An ``Include`` inside a ``Host`` or ``Match`` block is conditional: the
    included file's own blocks apply only where the enclosing one does, so
    each of their headers is rewritten as a ``Match`` that requires both
    (``_scope`` holds the criteria of the enclosing block).
    """
    if _depth > _INCLUDE_MAX_DEPTH:
        raise ValueError(f"ssh_config Include nesting deeper than {_INCLUDE_MAX_DEPTH}")
    lines: "list[str]" = []
    scope = _scope
    header = _scope_header(scope)
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = _DIRECTIVE.match(line)
            keyword = match.group(1).lower() if match else ""
            if keyword in ("host", "match"):
                criteria = (
                    _host_criterion(match.group(2))
                    if keyword == "host"
                    else _match_criterion(match.group(2))
                )
                scope = _scope + criteria
                header = line.strip() if not _scope else _scope_header(scope)
                lines.append(line if not _scope else header + "\n")
                continue
            if keyword != "include":
                lines.append(line)
                continue
            for pattern in match.group(2).split():
                pattern = _pathlib.Path(pattern.strip('"')).expanduser()
                if not pattern.anchor:
                    pattern = _pathlib.Path.home() / ".ssh" / pattern
                for included in sorted(_glob.glob(str(pattern))):
                    if _pathlib.Path(included).is_file():
                        lines.extend(_expand_includes(included, _depth + 1, scope))
            lines.append(header + "\n")
    return lines
