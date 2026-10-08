"""Probes for what the machine running the tests can do, and the skip markers
built on them. A test that needs a junction, a named pipe or POSIX permission
bits asks whether it can have one, not which operating system it runs on."""

from __future__ import annotations

import os
import stat
import tempfile

import pytest


def can_make_junctions() -> bool:
    """A directory junction can be created and read as a directory."""
    try:
        import _winapi

        create = _winapi.CreateJunction
    except (ImportError, AttributeError):
        return False
    with tempfile.TemporaryDirectory() as scratch:
        target = os.path.join(scratch, "target")
        link = os.path.join(scratch, "link")
        os.mkdir(target)
        try:
            create(target, link)
        except OSError:
            return False
        return os.path.isdir(link)


def can_make_fifos() -> bool:
    return hasattr(os, "mkfifo")


def keeps_posix_modes() -> bool:
    """`chmod` sets the permission bits `stat` then reports, and the process
    has a umask."""
    if not hasattr(os, "umask"):
        return False
    with tempfile.TemporaryDirectory() as scratch:
        path = os.path.join(scratch, "file")
        with open(path, "w"):
            pass
        os.chmod(path, 0o640)
        return stat.S_IMODE(os.stat(path).st_mode) == 0o640


def stores_question_marks() -> bool:
    """A file name holding `?` can be created here."""
    with tempfile.TemporaryDirectory() as scratch:
        try:
            with open(os.path.join(scratch, "q?x"), "w"):
                pass
        except OSError:
            return False
        return True


requires_junctions = pytest.mark.skipif(
    not can_make_junctions(), reason="this system cannot create directory junctions"
)
requires_fifos = pytest.mark.skipif(
    not can_make_fifos(), reason="this system has no named pipes (os.mkfifo)"
)
requires_posix_modes = pytest.mark.skipif(
    not keeps_posix_modes(),
    reason="this file system does not keep POSIX permission bits",
)
