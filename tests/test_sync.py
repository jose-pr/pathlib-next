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
