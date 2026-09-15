"""PathSyncer symlink sync across two different backends: SftpPath (over a
mocked, paramiko-shaped client -- no paramiko, no server) and LocalPath or
MemPath.

Split out of test_sync.py, where a mid-module `pytest.importorskip("paramiko")`
(which these tests never needed) and the `pathlib_next.uri` import skipped or
un-collected every core PathSyncer test with them.
"""

import os
import stat as _stat_mod

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.uri.schemes.sftp import BaseSftpBackend, SftpPath
from pathlib_next.utils.sync import PathAndStat, PathSyncer


def checksum(entry: PathAndStat):
    return entry.stat.st_size


def _relative_symlink_source(tmp_path):
    """A LocalPath symlink with a RELATIVE target, or skip if unsupported."""
    real = tmp_path / "real.txt"
    real.write_text("payload")
    link = tmp_path / "link.txt"
    try:
        os.symlink("real.txt", link)  # relative target, not resolved
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")
    return pathlib_next.LocalPath(link)


# --- cross-backend symlink sync (one side supports symlink_to(), the other
# doesn't / a different implementation) --------------------------------------
# Reuses tests/test_sftp.py's _FakeBackend/mocked-client pattern (no real
# SFTP server) so `SftpPath` can stand in as a *different* backend from
# LocalPath that also implements readlink()/symlink_to() (per
# docs/divergences.md, sftp: is the one other backend with real symlink
# support).


class _FakeSymlinkAttr:
    def __init__(self, filename, st_mode):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = 0
        self.st_mtime = 0


class _FakeSymlinkSftpClient:
    """Minimal paramiko-shaped SFTPClient: one symlink at /link.txt
    pointing (raw, relative) at "real.txt", plus the real file it targets.
    Records symlink() calls so tests can assert what target was created."""

    def __init__(self):
        self.symlink_calls = []
        self._files = {"/real.txt": b"payload"}
        self._link_target = {"/link.txt": "real.txt"}

    def _mode_for(self, path):
        if path in self._link_target:
            return _stat_mod.S_IFLNK | 0o777
        if path in self._files:
            return _stat_mod.S_IFREG | 0o644
        raise FileNotFoundError(path)

    def lstat(self, path):
        return _FakeSymlinkAttr(path.rsplit("/", 1)[-1], self._mode_for(path))

    def stat(self, path):
        if path in self._link_target:
            path = "/" + self._link_target[path]
        return _FakeSymlinkAttr(path.rsplit("/", 1)[-1], self._mode_for(path))

    def readlink(self, path):
        return self._link_target[path]

    def symlink(self, target, path):
        self.symlink_calls.append((target, path))
        self._link_target[path] = target

    def listdir_attr(self, path):
        return []

    def open(self, path, mode, buffering):
        raise NotImplementedError


class _FakeSymlinkBackend(BaseSftpBackend):
    def __init__(self):
        self._client = _FakeSymlinkSftpClient()

    def client(self, source):
        return self._client


def _fake_sftp(path, backend):
    return SftpPath(path, backend=backend)


def test_sync_symlink_preserve_sftp_source_to_local_target(tmp_path):
    # SFTP (supports symlink_to) -> Local (supports symlink_to): the
    # supported/supported case across two DIFFERENT backend types.
    backend = _FakeSymlinkBackend()
    source = _fake_sftp("sftp://host/link.txt", backend=backend)
    target = pathlib_next.LocalPath(tmp_path / "link_copy.txt")

    PathSyncer(checksum, follow_symlinks=False).sync(source, target)

    assert target.is_symlink()
    assert target.readlink().as_posix() == "real.txt"


def test_sync_symlink_preserve_local_source_to_sftp_target(tmp_path):
    # Local (supports symlink_to) -> SFTP (supports symlink_to): the other
    # direction of the supported/supported cross-backend case.
    source = _relative_symlink_source(tmp_path)
    backend = _FakeSymlinkBackend()
    target = _fake_sftp("sftp://host/link_copy.txt", backend=backend)

    PathSyncer(checksum, follow_symlinks=False).sync(source, target)

    assert backend._client.symlink_calls == [("real.txt", "/link_copy.txt")]


def test_sync_symlink_preserve_cross_backend_unsupported_target_raises(
    tmp_path,
):
    # SFTP source (supports symlink_to) -> MemPath target (does not):
    # the documented behavior is NotImplementedError surfaced through
    # ignore_error/hook(), not a silent skip or crash.
    backend = _FakeSymlinkBackend()
    source = _fake_sftp("sftp://host/link.txt", backend=backend)
    target = MemPath("/link_copy.txt")

    with pytest.raises(NotImplementedError):
        PathSyncer(checksum, follow_symlinks=False).sync(source, target)
    assert not target.exists()
