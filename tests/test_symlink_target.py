"""The text a `symlink_to()` target is stored as."""

from __future__ import annotations

import os
import pathlib

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath, MemPathBackend

TEXTS = ["./t/", "t//x", "../up/./t", "t/"]

# Windows cannot resolve a relative target spelled with "/", so LocalPath
# turns the text into the path class's own form there.
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="a Windows link needs its own separators"
)


def _stored(link: pathlib.Path) -> str:
    return os.readlink(link)


@posix_only
@pytest.mark.parametrize("text", TEXTS)
def test_localpath_stores_the_target_text_as_pathlib_does(tmp_path, text):
    stdlib = tmp_path / "stdlib"
    ours = tmp_path / "ours"
    try:
        stdlib.symlink_to(text)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")
    pathlib_next.LocalPath(ours).symlink_to(text)
    assert _stored(ours) == _stored(stdlib)


@posix_only
def test_a_downstream_local_class_stores_the_target_text_as_pathlib_does(tmp_path):
    from pathlib_next.fspath import _BaseFSPathname
    from pathlib_next.path import Path

    class Downstream(
        pathlib.WindowsPath if os.name == "nt" else pathlib.PosixPath,
        Path,
        _BaseFSPathname,
    ):
        __slots__ = ()

    stdlib = tmp_path / "stdlib"
    try:
        stdlib.symlink_to("./t/")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")
    Downstream(tmp_path / "ours").symlink_to("./t/")
    assert _stored(tmp_path / "ours") == _stored(stdlib)


def test_a_relative_target_written_with_slashes_resolves(tmp_path):
    (tmp_path / "real.txt").write_text("real")
    (tmp_path / "sub").mkdir()
    link = pathlib_next.LocalPath(tmp_path / "sub" / "link")
    try:
        link.symlink_to("../real.txt")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")
    assert link.read_text() == "real"


def test_a_str_target_of_a_memory_path_keeps_the_backend():
    seen = []

    class Recording(MemPath):
        __slots__ = ()

        def _symlink_to(self, target, target_is_directory=False):
            seen.append((target, target_is_directory))

    backend = MemPathBackend()
    Recording("/a", backend=backend).symlink_to("t")
    ((target, _),) = seen
    assert isinstance(target, Recording)
    assert target.backend is backend
    assert target.as_posix() == "t"


def test_a_path_target_is_handed_over_as_given():
    seen = []

    class Recording(MemPath):
        __slots__ = ()

        def _symlink_to(self, target, target_is_directory=False):
            seen.append(target)

    backend = MemPathBackend()
    target = Recording("/elsewhere", backend=backend)
    Recording("/a", backend=backend).symlink_to(target)
    assert seen == [target]
    assert seen[0] is target
