"""Downstream-subclass MRO precedence regressions.

Concrete path classes are built by mixing a `pathlib` class with
`pathlib_next.Path` -- our own `LocalPath` does it, and the documented
downstream recipe (`class X(PosixPathname, Path)`) does it transitively.
Python then resolves each method to whichever base declares it first, and
*which library wins changes with the interpreter version*, because stdlib
`pathlib` keeps gaining and changing methods.

That bug has two opposite modes, and both are covered here:

* NEW stdlib overriding us -- `copy`/`move` landed in CPython 3.14 and
  expect the private `_copy_from` protocol. Non-local downstream backends
  crash loudly (`AttributeError: ... has no attribute '_copy_from'`);
  local-backed ones SILENTLY succeed with stdlib's different metadata
  semantics (stdlib preserves timestamps, ours preserves st_mode only).
* OLD stdlib lacking our keywords -- `exists(follow_symlinks=)` is 3.12+,
  `read_text`/`write_text`'s `newline=` is 3.13+, and `rglob`'s
  `include_hidden=`/`recursive=`/`dironly=` extensions never existed in
  stdlib at all. On the 3.9 floor those keywords raise `TypeError`.
  `symlink_to`'s `force=` is the same mode in its most permanent form: no
  stdlib version has ever accepted it, so the guard is what keeps
  `force=True` from raising `TypeError` on local-backed classes while
  every remote backend honors it.

The critical assertion style here is **which implementation ran**
(`__module__`), not merely "it did not raise": a does-it-crash test passes
for local-backed downstream classes while the bug is fully present.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

import pathlib_next
from pathlib_next import Path, PosixPathname, WindowsPathname
from pathlib_next.fspath import _BaseFSPathname
from pathlib_next.mempath import MemPath

# Every operation whose implementation must come from pathlib_next no matter
# where stdlib sits in a subclass's MRO.
GUARDED = (
    "copy",
    "move",
    "exists",
    "rglob",
    "read_text",
    "write_text",
    "symlink_to",
)


def _impl_module(cls, name):
    return getattr(getattr(cls, name), "__module__", "")


# --- the documented downstream composition, on a NON-local backend --------


class DownstreamMemPath(MemPath):
    """A downstream concrete path over a non-local (in-memory) backend."""

    __slots__ = ()


class DownstreamPosixPath(PosixPathname, Path):
    """The exact composition pattern the docs recommend for a custom
    backend. On 3.14 this is where stdlib `copy`/`move` would win."""

    __slots__ = ()


class DownstreamWindowsPath(WindowsPathname, Path):
    __slots__ = ()


class DownstreamConcreteLocal(
    pathlib.WindowsPath if os.name == "nt" else pathlib.PosixPath,
    Path,
    _BaseFSPathname,
):
    """A downstream local class mixing a CONCRETE stdlib path with ours --
    the shape that actually reproduced the reported defect, and the one
    where the 3.14 failure is silent rather than loud."""

    __slots__ = ()


DOWNSTREAM_CLASSES = [
    DownstreamMemPath,
    DownstreamPosixPath,
    DownstreamWindowsPath,
    DownstreamConcreteLocal,
    pathlib_next.LocalPath,
]


@pytest.mark.parametrize("cls", DOWNSTREAM_CLASSES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("name", GUARDED)
def test_downstream_subclass_resolves_pathlib_next_implementation(cls, name):
    """Assert WHICH implementation ran, not merely that it did not raise."""
    module = _impl_module(cls, name)
    assert module.startswith("pathlib_next"), (
        f"{cls.__name__}.{name} resolved to {module!r} instead of "
        f"pathlib_next (Python {sys.version.split()[0]}); stdlib pathlib "
        f"won the MRO"
    )


def test_non_local_downstream_copy_does_not_need_copy_from():
    """The loud mode: on 3.14 stdlib's copy would demand `_copy_from`."""
    src = DownstreamMemPath("/src.txt")
    src.write_text("payload")
    dst = DownstreamMemPath("/dst.txt", backend=src.backend)
    src.copy(dst)
    assert dst.read_text() == "payload"


def test_local_downstream_copy_keeps_pathlib_next_metadata_semantics(tmp_path):
    """The SILENT mode: a local-backed downstream class copies without
    raising on every version, so only the observable metadata consequence
    distinguishes stdlib's implementation from ours.

    pathlib_next's copy preserves st_mode only -- never timestamps. Stdlib
    3.14's `preserve_metadata=True` path does preserve mtime, which is what
    made mtime-based syncs converge on 3.14 and never converge on <=3.13.
    """
    src = pathlib_next.LocalPath(tmp_path / "src.txt")
    src.write_text("payload")
    backdated = 100000.0
    os.utime(os.fspath(src), (backdated, backdated))

    dst = pathlib_next.LocalPath(tmp_path / "dst.txt")
    src.copy(dst)

    assert dst.read_text() == "payload"
    # Version-independent invariant: our copy does NOT carry mtime over.
    assert dst.stat().st_mtime != pytest.approx(backdated, abs=1)


# --- OLD-stdlib direction: keywords the protocols promise ------------------


def test_local_exists_accepts_follow_symlinks(tmp_path):
    """`Stat.exists(*, follow_symlinks=True)` is protocol; stdlib only
    gained the keyword in 3.12, so on the floor this used to raise
    `TypeError`. A call with NO arguments passes on every interpreter while
    the defect is fully present -- the keyword is the whole point."""
    p = pathlib_next.LocalPath(tmp_path / "f.txt")
    p.write_text("x")
    assert p.exists(follow_symlinks=True) is True
    assert p.exists(follow_symlinks=False) is True
    missing = pathlib_next.LocalPath(tmp_path / "nope.txt")
    assert missing.exists(follow_symlinks=False) is False


def test_local_exists_on_dangling_symlink(tmp_path):
    """The case the downstream project actually needed: a dangling link
    exists as a link (follow_symlinks=False) but not as a target."""
    link = tmp_path / "dangling"
    try:
        os.symlink(tmp_path / "missing-target", link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")
    p = pathlib_next.LocalPath(link)
    assert p.exists(follow_symlinks=False) is True
    assert p.exists(follow_symlinks=True) is False


def test_local_exists_maps_oserror_to_false(tmp_path):
    """`exists()` must swallow OSError and report False (pathlib parity)."""

    class Boom(pathlib_next.LocalPath):
        __slots__ = ()

        def stat(self, *, follow_symlinks=True):
            raise PermissionError("denied")

    assert Boom(tmp_path / "x").exists(follow_symlinks=False) is False


def test_local_rglob_accepts_pathlib_next_extensions(tmp_path):
    """`include_hidden=`/`recursive=`/`dironly=` never existed in stdlib's
    rglob, and `glob` was already routed on LocalPath while `rglob` was
    not -- a real gap on BOTH interpreters."""
    root = pathlib_next.LocalPath(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("a")
    (tmp_path / ".hidden.txt").write_text("h")

    assert [p.name for p in root.rglob("a.txt")] == ["a.txt"]
    names = {p.name for p in root.rglob("*", include_hidden=True)}
    assert ".hidden.txt" in names
    assert {p.name for p in root.rglob("*", dironly=True)} == {"sub"}


def test_local_read_write_text_accept_newline(tmp_path):
    """`newline=` is 3.13+ in stdlib's read_text/write_text but is part of
    this library's BinaryOpen protocol on every supported version."""
    p = pathlib_next.LocalPath(tmp_path / "nl.txt")
    p.write_text("a\nb", newline="\r\n")
    assert p.read_bytes() == b"a\r\nb"
    assert p.read_text(newline="") == "a\r\nb"


def test_subclass_may_still_override_guarded_operations(tmp_path):
    """The precedence guard must not clobber a downstream's OWN override --
    it only displaces implementations coming from outside pathlib_next."""

    class CustomCopy(pathlib_next.LocalPath):
        __slots__ = ()
        called = False

        def copy(self, target, **kwargs):
            type(self).called = True
            return super().copy(target, **kwargs)

    src = CustomCopy(tmp_path / "s.txt")
    src.write_text("v")
    dst = CustomCopy(tmp_path / "d.txt")
    src.copy(dst)
    # The subclass's own copy() ran (it is NOT replaced by the guard), and
    # its super() call still reaches pathlib_next rather than stdlib.
    assert CustomCopy.called is True
    assert CustomCopy.copy is vars(CustomCopy)["copy"]
    assert dst.read_text() == "v"


def test_downstream_mixin_composition_keeps_precedence():
    """The real downstream shape: a behavior mixin combined with a pathname
    mixin and `Path` (e.g. hostctl's `class X(_Mixin, PosixPathname, Path)`).
    The mixin does not define these operations, so pathlib_next must still
    win -- on every interpreter."""

    class _Mixin:
        __slots__ = ()

    class Composed(_Mixin, PosixPathname, Path):
        __slots__ = ()

    for name in GUARDED:
        assert _impl_module(Composed, name).startswith("pathlib_next"), name


def test_mixin_defined_operation_is_not_displaced():
    """A downstream mixin that deliberately implements one of these
    operations keeps its implementation."""

    class CopyMixin:
        __slots__ = ()

        def copy(self, target, **kwargs):  # pragma: no cover - identity only
            raise AssertionError("sentinel")

    class Composed(CopyMixin, PosixPathname, Path):
        __slots__ = ()

    assert Composed.copy is CopyMixin.copy
