"""The exceptions both SSH libraries raise for a connection, a login or a host
key, as the `OSError` subclasses `SftpPath` raises instead.

Neither SSH library is imported here: each backend decides which of its own
exceptions is which and builds the result with these functions, so a failure
reads the same on both. The library exception is never chained: its text can
carry credentials. The text of the library exceptions that `aborted()` and
`request_failed()` reduce to a class name is logged at DEBUG on the logger
`pathlib_next.sftp`.
"""

from __future__ import annotations

import errno as _errno
import logging as _logging
import typing as _ty

if _ty.TYPE_CHECKING:
    from ... import Source


_logger = _logging.getLogger("pathlib_next.sftp")


def _log_library_error(what: str, library_error: BaseException) -> None:
    # A DEBUG record, not part of the exception: the library's text can carry
    # credentials, and only a handler the program installed ever sees it.
    _logger.debug("%s: %s: %s", what, type(library_error).__name__, library_error)


class SftpAuthenticationError(PermissionError):
    """The server refused the credentials offered (`errno.EACCES`)."""


class SftpHostKeyError(ConnectionError):
    """The server's host key was unknown, changed or not accepted, so no
    credential was sent (`errno.ECONNABORTED`)."""


def _where(source: "Source | None") -> str:
    if not source or not source.host:
        return "the SFTP server"
    user, _password = source.parsed_userinfo()
    port = f":{source.port}" if source.port else ""
    return f"{user + '@' if user else ''}{source.host}{port}"


def os_error(
    cls: "type[OSError]",
    code: int,
    strerror: str,
    filename: "str | None" = None,
    filename2: "str | None" = None,
) -> OSError:
    """`cls(code, strerror, filename)`, pathlib's shape, with `filename2` set
    afterwards: the five-argument form prints a winerror of None as
    "[WinError None]" on Windows."""
    if filename is None:
        return cls(code, strerror)
    result = cls(code, strerror, filename)
    result.filename2 = filename2
    return result


def bare_failure(
    message: str, filename: "str | None" = None, filename2: "str | None" = None
) -> OSError:
    """An `OSError` without an errno, for a status that has no POSIX
    equivalent: `SftpPath` reads `errno is None` as "consult the entry
    itself"."""
    if filename is None:
        return OSError(message)
    result = OSError(None, message, filename)
    result.filename2 = filename2
    return result


def lost() -> ConnectionResetError:
    """The connection went away while a request was in flight."""
    return ConnectionResetError(_errno.ECONNRESET, "SFTP connection lost")


def aborted(library_error: BaseException) -> ConnectionAbortedError:
    """The SSH handshake or session failed for a reason other than a login or
    a host key; only the name of the library's exception is carried over, and
    its text is logged at DEBUG."""
    _log_library_error("SFTP connection failed", library_error)
    return ConnectionAbortedError(
        _errno.ECONNABORTED,
        f"SFTP connection failed ({type(library_error).__name__})",
    )


def request_failed(library_error: BaseException) -> OSError:
    """A failure of the SSH library that is neither the connection, the login
    nor the host key; like `aborted()`, only the exception's name is carried."""
    _log_library_error("SFTP request failed", library_error)
    return OSError(_errno.EIO, f"SFTP request failed ({type(library_error).__name__})")


def login_refused(source: "Source | None" = None) -> SftpAuthenticationError:
    return SftpAuthenticationError(
        _errno.EACCES, f"SFTP authentication to {_where(source)} was refused"
    )


def host_key_refused(source: "Source | None" = None) -> SftpHostKeyError:
    return SftpHostKeyError(
        _errno.ECONNABORTED,
        f"the host key of {_where(source)} is unknown, changed or not accepted",
    )
