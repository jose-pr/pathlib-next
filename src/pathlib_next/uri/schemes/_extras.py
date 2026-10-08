"""Client libraries a scheme needs and the extra that installs them.

A scheme class always registers and constructs; a path does pure-path work
(joins, parents, comparison) without its client library, and the first
operation that needs it raises the `ImportError` built here.
"""

from __future__ import annotations

import importlib as _importlib


def missing_extra(package: str, extra: str, cause: "BaseException | None" = None):
    """The `ImportError` for a client library that cannot be imported, naming
    the extra to install."""
    reason = f" ({cause})" if cause is not None else ""
    return ImportError(
        f"{package} could not be imported{reason}; the '{extra}' extra provides "
        f'it: pip install "pathlib-next[{extra}]"',
        name=package,
    )


class _Unavailable:
    """Stands in for a client module that cannot be imported: reading any
    attribute of it raises the `ImportError` of `missing_extra()`."""

    __slots__ = ("_package", "_extra", "_cause")

    def __init__(self, package: str, extra: str, cause: BaseException):
        self._package = package
        self._extra = extra
        self._cause = cause

    def __getattr__(self, name: str):
        if name.startswith("__"):
            # Introspection (`inspect`, `copy`, doctest) probes dunders.
            raise AttributeError(name)
        raise missing_extra(self._package, self._extra, self._cause) from self._cause

    def __repr__(self) -> str:
        return f"<{self._package}: not installed, needs the {self._extra!r} extra>"


def import_or_stub(package: str, extra: str):
    """The module `package`, or a stand-in that raises `missing_extra()` on
    first use when it cannot be imported."""
    try:
        return _importlib.import_module(package)
    except ImportError as error:
        return _Unavailable(package, extra, error)


def import_client(package: str, extra: str):
    """The module `package`, imported at the point of use; the `ImportError`
    of `missing_extra()` when it cannot be."""
    try:
        return _importlib.import_module(package)
    except ImportError as error:
        raise missing_extra(package, extra, error) from error
