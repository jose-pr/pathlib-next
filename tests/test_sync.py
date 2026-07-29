import logging

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.utils.stat import FileStat
from pathlib_next.utils.sync import PathAndStat, PathSyncer


def checksum(entry: PathAndStat):
    return entry.stat.st_size


def _mem_tree():
    root = MemPath("/")
    (root / "a.txt").write_text("aaa")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("bb")
    return root


def test_sync_mem_to_local_creates_tree(tmp_path):
    source = _mem_tree()
    target = pathlib_next.LocalPath(tmp_path)
    syncer = PathSyncer(checksum)
    syncer.sync(source, target)
    assert (tmp_path / "a.txt").read_text() == "aaa"
    assert (tmp_path / "sub" / "b.txt").read_text() == "bb"


def test_sync_dry_run_makes_no_changes(tmp_path):
    source = _mem_tree()
    target = pathlib_next.LocalPath(tmp_path)
    syncer = PathSyncer(checksum)
    syncer.sync(source, target, dry_run=True)
    assert list(tmp_path.iterdir()) == []


def test_sync_skips_matching_checksum(tmp_path):
    source = _mem_tree()
    target = pathlib_next.LocalPath(tmp_path)
    (tmp_path / "a.txt").write_text("aaa")  # already matches source's size
    syncer = PathSyncer(checksum)
    events = []
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)
    from pathlib_next.utils.sync import SyncEvent

    # a.txt already has the same checksum -- no Copy event for it, only
    # for sub/b.txt.
    assert events.count(SyncEvent.Copy) == 1


def test_sync_remove_missing_deletes_extra_target_files(tmp_path):
    source = _mem_tree()
    target = pathlib_next.LocalPath(tmp_path)
    (tmp_path / "extra.txt").write_text("gone soon")
    syncer = PathSyncer(checksum, remove_missing=True)
    syncer.sync(source, target)
    assert not (tmp_path / "extra.txt").exists()
    assert (tmp_path / "a.txt").exists()


def test_sync_remove_missing_false_keeps_extra_files(tmp_path):
    source = _mem_tree()
    target = pathlib_next.LocalPath(tmp_path)
    (tmp_path / "extra.txt").write_text("stays")
    syncer = PathSyncer(checksum, remove_missing=False)
    syncer.sync(source, target)
    assert (tmp_path / "extra.txt").exists()


def test_sync_ignore_error_callable_invoked(tmp_path):
    def bad_checksum(entry):
        raise RuntimeError("boom")

    calls = []
    syncer = PathSyncer(
        bad_checksum,
        ignore_error=lambda err, source, target, event: calls.append(err) or True,
    )
    source = _mem_tree()
    target = pathlib_next.LocalPath(tmp_path)
    # a.txt already exists as a file with a checksum mismatch (forces the
    # checksum() call, which raises)
    (tmp_path / "a.txt").write_text("different")
    syncer.sync(source, target)
    assert len(calls) >= 1
    assert isinstance(calls[0], RuntimeError)


def test_sync_local_to_local(tmp_path):
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    (src_dir / "f.txt").write_text("data")
    syncer = PathSyncer(checksum)
    syncer.sync(pathlib_next.LocalPath(src_dir), pathlib_next.LocalPath(dst_dir))
    assert (dst_dir / "f.txt").read_text() == "data"


# --- N2: PathSyncer.log() must use logging, not print() ---


def test_sync_log_uses_logging_not_print(tmp_path, capsys, caplog):
    source = _mem_tree()
    target = pathlib_next.LocalPath(tmp_path)
    syncer = PathSyncer(checksum)
    with caplog.at_level(logging.INFO, logger="pathlib_next.sync"):
        syncer.sync(source, target)
    assert capsys.readouterr().out == ""  # nothing printed to stdout
    assert any(r.name == "pathlib_next.sync" for r in caplog.records)


# --- B22: PathAndStat.__getattr__ raises AttributeError for unknown attrs ---


def test_pathandstat_unknown_attr_raises():
    root = MemPath("/")
    (root / "f.txt").write_text("x")
    pas = PathAndStat(root / "f.txt")
    with pytest.raises(AttributeError):
        pas.totally_unknown_attribute


def test_pathandstat_is_prefixed_attr_delegates_to_stat():
    root = MemPath("/")
    (root / "f.txt").write_text("x")
    pas = PathAndStat(root / "f.txt")
    assert pas.is_file() is True
    assert pas.is_dir() is False


def test_pathandstat_missing_path_is_methods_return_false():
    pas = PathAndStat(MemPath("/missing.txt"))
    assert pas.exists() is False
    assert pas.is_file() is False


def test_sync_default_checksum(tmp_path):
    source = _mem_tree()
    target = pathlib_next.LocalPath(tmp_path)
    syncer = PathSyncer()
    syncer.sync(source, target)
    assert (tmp_path / "a.txt").read_text() == "aaa"
    assert (tmp_path / "sub" / "b.txt").read_text() == "bb"


def test_sync_reuses_scandir_metadata_when_not_following_symlinks():
    class CountingMemPath(MemPath):
        stat_calls = 0

        def stat(self, *, follow_symlinks=True):
            type(self).stat_calls += 1
            return super().stat(follow_symlinks=follow_symlinks)

        def _scandir(self):
            for child in self.iterdir():
                yield child.name, FileStat(is_dir=child.name == "sub")

    source = CountingMemPath("/")
    (source / "a.txt").write_text("aaa")
    (source / "b.txt").write_text("bbb")
    target = CountingMemPath("/target")
    target.mkdir()

    CountingMemPath.stat_calls = 0
    PathSyncer(lambda entry: entry.stat.st_size, follow_symlinks=False).sync(
        source, target, dry_run=True
    )

    # Root source/target are still statted at sync start, but source
    # children should be built from _scandir() metadata rather than
    # refreshed one-by-one.
    assert CountingMemPath.stat_calls == 4


# --- PathSyncer ignore_error: bool-or-callable, and the symlink branch -----
# Regression for the shadowing defect: `sync()`'s `ignore_error` parameter
# defaulted to the bool `False` and the symlink branch CALLED it directly,
# so `PathSyncer(ignore_error=True).sync(...)` on a symlink source raised
# `TypeError: 'bool' object is not callable` instead of the intended
# `NotImplementedError`. The parameter now defaults to None ("use the
# instance policy") and every branch consults one resolved callable.
#
# NOTE: as of the pathsyncer_symlinks plan, `symlink_mode` defaults to
# "preserve" (create a matching symlink on target) instead of always
# raising -- this whole test group forces `symlink_mode="reject"` via
# `_symlink_syncer()` to keep regression-testing the OLD unconditional-raise
# behavior, which remains available as an explicit opt-out. See the
# "symlink preserve mode" section below for the new default's own coverage.


def _symlink_source(tmp_path):
    """A LocalPath pointing at a symlink, or skip if unsupported."""
    import os

    real = tmp_path / "real.txt"
    real.write_text("x")
    link = tmp_path / "link.txt"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")
    return pathlib_next.LocalPath(link)


def _symlink_syncer(**kwargs):
    # follow_symlinks=False so `source.is_symlink()` is true and the symlink
    # branch is actually reached (the default resolves through the link).
    # symlink_mode="reject" restores the pre-pathsyncer_symlinks-plan
    # behavior (unconditional NotImplementedError) that this whole test
    # group is regression-testing -- symlink_mode's own default
    # ("preserve") wouldn't raise at all, so there'd be no error for
    # ignore_error to tolerate/reject.
    kwargs.setdefault("symlink_mode", "reject")
    return PathSyncer(checksum, follow_symlinks=False, **kwargs)


def test_sync_symlink_ignore_error_ctor_bool_true(tmp_path):
    source = _symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "out.txt")
    # Was TypeError: 'bool' object is not callable.
    _symlink_syncer(ignore_error=True).sync(source, target)


def test_sync_symlink_ignore_error_param_bool_true(tmp_path):
    source = _symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "out.txt")
    _symlink_syncer().sync(source, target, ignore_error=True)


def test_sync_symlink_ignore_error_default_raises(tmp_path):
    source = _symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "out.txt")
    with pytest.raises(NotImplementedError):
        _symlink_syncer().sync(source, target)


def test_sync_symlink_ignore_error_bool_false_raises(tmp_path):
    source = _symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "out.txt")
    with pytest.raises(NotImplementedError):
        _symlink_syncer(ignore_error=False).sync(source, target)


def test_sync_symlink_ignore_error_callable_true_tolerates(tmp_path):
    source = _symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "out.txt")
    calls = []
    _symlink_syncer().sync(
        source,
        target,
        ignore_error=lambda err, s, t, event: calls.append(err) or True,
    )
    assert len(calls) == 1
    assert isinstance(calls[0], NotImplementedError)


def test_sync_symlink_ignore_error_callable_false_raises(tmp_path):
    source = _symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "out.txt")
    with pytest.raises(NotImplementedError):
        _symlink_syncer().sync(source, target, ignore_error=lambda *args: False)


def test_sync_symlink_ctor_policy_reaches_symlink_branch(tmp_path):
    # The constructor-supplied policy used to be silently ineffective for
    # this branch, because only the (shadowing) parameter was consulted.
    source = _symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "out.txt")
    calls = []
    _symlink_syncer(
        ignore_error=lambda err, s, t, event: calls.append(err) or True
    ).sync(source, target)
    assert len(calls) == 1


def test_sync_param_ignore_error_none_uses_instance_policy(tmp_path):
    # None means "use the policy given to __init__", not "override with
    # False" (the old default silently shadowed the constructor).
    source = _symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "out.txt")
    _symlink_syncer(ignore_error=True).sync(source, target, ignore_error=None)


# --- symlink preserve mode (pathsyncer_symlinks plan) -----------------------
# symlink_mode defaults to "preserve": PathSyncer(follow_symlinks=False)
# syncing a symlink source now creates a matching symlink on target instead
# of always raising. symlink_mode="reject" is the opt-out that restores the
# old unconditional-NotImplementedError behavior (covered above).

import os as _os


def _relative_symlink_source(tmp_path):
    """A LocalPath symlink with a RELATIVE target, or skip if unsupported."""
    real = tmp_path / "real.txt"
    real.write_text("payload")
    link = tmp_path / "link.txt"
    try:
        _os.symlink("real.txt", link)  # relative target, not resolved
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")
    return pathlib_next.LocalPath(link)


def _preserve_syncer(**kwargs):
    return PathSyncer(checksum, follow_symlinks=False, **kwargs)


def test_symlink_mode_invalid_value_raises():
    with pytest.raises(ValueError):
        PathSyncer(symlink_mode="nope")


def test_sync_symlink_preserve_default_creates_relative_symlink(tmp_path):
    source = _relative_symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "link_copy.txt")
    _preserve_syncer().sync(source, target)  # symlink_mode defaults "preserve"
    assert target.is_symlink()
    # Raw, unresolved target preserved exactly -- not resolved against
    # source's parent.
    assert target.readlink().as_posix() == "real.txt"
    # The link is live (points at a real sibling file), so following it
    # from the target's own directory reads the same payload.
    assert (tmp_path / "link_copy.txt").read_text() == "payload"


def test_sync_symlink_preserve_explicit_mode_matches_default(tmp_path):
    source = _relative_symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "link_copy.txt")
    _preserve_syncer(symlink_mode="preserve").sync(source, target)
    assert target.is_symlink()
    assert target.readlink().as_posix() == "real.txt"


def test_sync_symlink_reject_mode_still_raises(tmp_path):
    # Regression guard: the OLD default behavior (unconditional
    # NotImplementedError) remains available verbatim via the explicit
    # opt-out, unchanged by the new "preserve" default.
    source = _relative_symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "link_copy.txt")
    with pytest.raises(NotImplementedError):
        _preserve_syncer(symlink_mode="reject").sync(source, target)
    assert not target.exists()


def test_sync_symlink_preserve_dangling_link(tmp_path):
    # Target of the link doesn't exist -- preserve mode still creates the
    # link on target, no attempt to validate/resolve it.
    link = tmp_path / "dangling.txt"
    try:
        _os.symlink("does_not_exist.txt", link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")
    source = pathlib_next.LocalPath(link)
    target = pathlib_next.LocalPath(tmp_path / "dangling_copy.txt")

    _preserve_syncer().sync(source, target)

    assert target.is_symlink()
    assert not target.exists()  # dangling on target too, as expected
    assert target.readlink().as_posix() == "does_not_exist.txt"


def test_sync_symlink_preserve_replaces_file_target(tmp_path):
    # Type mismatch: target exists as a regular file where source is a
    # symlink -- preserve mode clears it first, then creates the symlink.
    source = _relative_symlink_source(tmp_path)
    target_path = tmp_path / "link_copy.txt"
    target_path.write_text("stale regular file content")
    target = pathlib_next.LocalPath(target_path)

    _preserve_syncer().sync(source, target)

    assert target.is_symlink()
    assert target.readlink().as_posix() == "real.txt"


def test_sync_symlink_preserve_replaces_dir_target(tmp_path):
    # Type mismatch: target exists as a directory where source is a
    # symlink -- preserve mode removes the dir tree first.
    source = _relative_symlink_source(tmp_path)
    target_path = tmp_path / "link_copy.txt"
    target_path.mkdir()
    (target_path / "nested.txt").write_text("stale nested content")
    target = pathlib_next.LocalPath(target_path)

    _preserve_syncer().sync(source, target)

    assert target.is_symlink()
    assert target.readlink().as_posix() == "real.txt"


def test_sync_symlink_preserve_unsupported_target_raises_not_implemented(
    tmp_path,
):
    # Target backend (MemPath) has no symlink_to() at all -- preserve mode
    # must raise NotImplementedError through the normal ignore_error/hook()
    # flow, not silently skip or crash with AttributeError.
    source = _relative_symlink_source(tmp_path)
    target = MemPath("/link_copy.txt")

    with pytest.raises(NotImplementedError):
        _preserve_syncer().sync(source, target)


def test_sync_symlink_preserve_unsupported_target_ignore_error_tolerates(
    tmp_path,
):
    source = _relative_symlink_source(tmp_path)
    target = MemPath("/link_copy.txt")
    calls = []

    _preserve_syncer(
        ignore_error=lambda err, s, t, event: calls.append(err) or True
    ).sync(source, target)

    assert len(calls) == 1
    assert isinstance(calls[0], NotImplementedError)
    assert not target.exists()


def test_sync_symlink_preserve_fires_symlink_event(tmp_path):
    source = _relative_symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "link_copy.txt")
    events = []
    syncer = _preserve_syncer()
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)

    from pathlib_next.utils.sync import SyncEvent

    assert SyncEvent.Symlink in events


def test_sync_symlink_preserve_dry_run_makes_no_changes(tmp_path):
    source = _relative_symlink_source(tmp_path)
    target = pathlib_next.LocalPath(tmp_path / "link_copy.txt")
    _preserve_syncer().sync(source, target, dry_run=True)
    assert not target.exists()
    assert not target.is_symlink()


# --- cross-backend symlink sync (one side supports symlink_to(), the other
# doesn't / a different implementation) --------------------------------------
# Reuses tests/test_sftp.py's _FakeBackend/mocked-client pattern (no real
# SFTP server) so `SftpPath` can stand in as a *different* backend from
# LocalPath that also implements readlink()/symlink_to() (per
# docs/divergences.md, sftp: is the one other backend with real symlink
# support).

pytest.importorskip("paramiko")

import stat as _stat_mod

from pathlib_next.uri.schemes.sftp import BaseSftpBackend, SftpPath


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
    # the documented Phase 1 behavior is NotImplementedError through
    # ignore_error/hook(), not a silent skip or crash.
    backend = _FakeSymlinkBackend()
    source = _fake_sftp("sftp://host/link.txt", backend=backend)
    target = MemPath("/link_copy.txt")

    with pytest.raises(NotImplementedError):
        PathSyncer(checksum, follow_symlinks=False).sync(source, target)
    assert not target.exists()
