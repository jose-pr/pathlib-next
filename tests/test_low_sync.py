"""PathSyncer low-severity soundness regressions (2026-09-15 deep review).

Tolerated errors leave a WARNING record and a `SyncEvent.Error` hook call; a
non-empty target directory is never deleted without `remove_missing`; hooks
receive `PathAndStat` and the real `dry_run` for every event; directory
symlinks are created as directory links; an identical link is left alone;
`PathAndStat` follows symlinks by default. Each test asserts the outcome on
the tree, the log record, or the call the backend received.
"""

import logging
import os
import sys

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.utils.stat import FileStat
from pathlib_next.utils.sync import PathAndStat, PathSyncer, SyncEvent

IS_WINDOWS = sys.platform == "win32"
LOGGER = "pathlib_next.sync"


def _size(entry):
    return entry.stat.st_size


def _symlink(target, link, *, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")


def _recorder():
    events = []

    def hook(source, target, event, dry_run):
        events.append((source, target, event, dry_run))

    return events, hook


def _warnings(caplog):
    return [
        r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING
    ]


# --- sync-ignored-errors-silent ---------------------------------------------


def _mem_source():
    source = MemPath("/src")
    source.mkdir()
    (source / "a.txt").write_text("a")
    (source / "b.txt").write_text("b")
    return source


def test_tolerated_copy_error_is_logged_and_reported(monkeypatch, caplog):
    source = _mem_source()
    target = MemPath("/dst")
    original = MemPath.copy

    def failing_copy(self, *args, **kwargs):
        if self.name == "a.txt":
            raise PermissionError(13, "denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemPath, "copy", failing_copy)
    events, hook = _recorder()

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        PathSyncer(_size, ignore_error=True, hook=hook).sync(source, target)

    assert (target / "b.txt").read_text() == "b"
    assert not (target / "a.txt").exists()
    records = _warnings(caplog)
    assert len(records) == 1
    assert records[0].exc_info[0] is PermissionError
    assert "a.txt" in records[0].getMessage()
    assert "Copy" in records[0].getMessage()
    errors = [(s, t) for s, t, e, _ in events if e is SyncEvent.Error]
    assert len(errors) == 1
    assert isinstance(errors[0][0], PathAndStat)
    assert errors[0][0].path.name == "a.txt"
    assert errors[0][1].path.name == "a.txt"
    # The failed Copy itself is not reported as done.
    copied = [s.path.name for s, _, e, _ in events if e is SyncEvent.Copy]
    assert copied == ["b.txt"]


def test_tolerated_compare_error_is_logged(caplog):
    source = _mem_source()
    target = MemPath("/dst")
    target.mkdir()
    (target / "a.txt").write_text("old")

    def bad_checksum(entry):
        raise RuntimeError("checksum failed")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        PathSyncer(bad_checksum, ignore_error=True).sync(source, target)

    records = _warnings(caplog)
    assert [r.exc_info[0] for r in records] == [RuntimeError]
    assert "Compare" in records[0].getMessage()
    assert (target / "a.txt").read_text() == "old"


def test_tolerated_root_error_is_logged_and_reported(caplog):
    events, hook = _recorder()
    target = MemPath("/dst")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        PathSyncer(_size, ignore_error=True, hook=hook).sync(
            MemPath("/does-not-exist"), target
        )

    assert [r.exc_info[0] for r in _warnings(caplog)] == [FileNotFoundError]
    assert [e for _, _, e, _ in events].count(SyncEvent.Error) == 1
    assert not target.exists()


def test_deep_tree_is_never_truncated_silently(caplog):
    # The traversal is recursive, so a tree deeper than the interpreter's
    # recursion limit fails with RecursionError. Tolerated, that must at
    # least leave a WARNING; an iterative traversal would reach the leaf.
    source = MemPath("/src")
    target = MemPath("/dst")
    deepest, leaf = source, target
    for depth in range(max(400, sys.getrecursionlimit() // 2)):
        deepest, leaf = deepest / f"d{depth}", leaf / f"d{depth}"
    deepest.mkdir(parents=True)
    (deepest / "leaf.txt").write_text("leaf")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        PathSyncer(_size, ignore_error=True).sync(source, target)

    if not (leaf / "leaf.txt").exists():
        assert RecursionError in [r.exc_info[0] for r in _warnings(caplog)]


def test_declined_error_is_not_logged_as_ignored(monkeypatch, caplog):
    source = _mem_source()

    def failing_copy(self, *args, **kwargs):
        raise PermissionError(13, "denied", str(self))

    monkeypatch.setattr(MemPath, "copy", failing_copy)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        with pytest.raises(PermissionError):
            PathSyncer(_size).sync(source, MemPath("/dst"))

    assert _warnings(caplog) == []


def test_callable_policy_signature_unchanged_and_error_still_logged(
    monkeypatch, caplog
):
    source = _mem_source()
    original = MemPath.copy

    def failing_copy(self, *args, **kwargs):
        if self.name == "a.txt":
            raise PermissionError(13, "denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemPath, "copy", failing_copy)
    calls = []

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        PathSyncer(
            _size,
            ignore_error=lambda error, s, t, event: calls.append(event) or True,
        ).sync(source, MemPath("/dst"))

    assert calls == [SyncEvent.Copy]
    assert len(_warnings(caplog)) == 1


# --- sync-type-change-deletes-tree-without-remove-missing -------------------


def _dir_to_file_trees():
    source = MemPath("/src")
    source.mkdir()
    (source / "reports").write_text("now a file")
    (source / "other.txt").write_text("other")
    target = MemPath("/dst")
    (target / "reports").mkdir(parents=True)
    for n in range(3):
        (target / "reports" / f"q{n}.csv").write_text(str(n))
    return source, target


def _csvs(target):
    return sorted(p.name for p in (target / "reports").iterdir())


def test_dir_to_file_without_remove_missing_raises_and_keeps_tree():
    source, target = _dir_to_file_trees()

    with pytest.raises(IsADirectoryError):
        PathSyncer(_size).sync(source, target)

    assert (target / "reports").is_dir()
    assert _csvs(target) == ["q0.csv", "q1.csv", "q2.csv"]


def test_dir_to_file_refusal_goes_through_policy_and_siblings_sync(caplog):
    source, target = _dir_to_file_trees()
    calls = []

    def policy(error, s, t, event):
        calls.append((type(error), t.path.name, event))
        return True

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        PathSyncer(_size, ignore_error=policy).sync(source, target)

    assert calls == [(IsADirectoryError, "reports", SyncEvent.TypeMismatch)]
    assert _csvs(target) == ["q0.csv", "q1.csv", "q2.csv"]
    assert (target / "other.txt").read_text() == "other"
    assert len(_warnings(caplog)) == 1


def test_dir_to_file_dry_run_takes_the_same_decision():
    source, target = _dir_to_file_trees()

    with pytest.raises(IsADirectoryError):
        PathSyncer(_size).sync(source, target, dry_run=True)

    assert _csvs(target) == ["q0.csv", "q1.csv", "q2.csv"]


def test_dir_to_file_with_remove_missing_replaces_tree():
    source, target = _dir_to_file_trees()

    PathSyncer(_size, remove_missing=True).sync(source, target)

    assert (target / "reports").read_text() == "now a file"


def test_empty_dir_to_file_without_remove_missing_is_replaced():
    source = MemPath("/src")
    source.mkdir()
    (source / "reports").write_text("now a file")
    target = MemPath("/dst")
    (target / "reports").mkdir(parents=True)

    PathSyncer(_size).sync(source, target)

    assert (target / "reports").read_text() == "now a file"


def test_dir_to_symlink_without_remove_missing_keeps_tree(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "real.txt").write_text("payload")
    _symlink("real.txt", source / "link")
    target = tmp_path / "dst"
    (target / "link").mkdir(parents=True)
    (target / "link" / "keep.txt").write_text("keep")

    with pytest.raises(IsADirectoryError):
        PathSyncer(_size, follow_symlinks=False).sync(
            pathlib_next.LocalPath(source), pathlib_next.LocalPath(target)
        )

    assert (target / "link" / "keep.txt").read_text() == "keep"
    assert not (target / "link").is_symlink()


# --- sync-hook-contract-start-event -------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_hook_receives_pathandstat_and_real_dry_run_for_every_event(dry_run):
    source = MemPath("/src")
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "f.txt").write_text("f")
    (source / "g.txt").write_text("g")
    target = MemPath("/dst")
    target.mkdir()
    (target / "stale.txt").write_text("stale")
    seen = []

    def hook(s, t, event, reported_dry_run):
        # Written against the declared type: reads the cached stat.
        seen.append((event, s.stat, t.stat, reported_dry_run))
        assert isinstance(s, PathAndStat) and isinstance(t, PathAndStat)

    PathSyncer(_size, remove_missing=True, hook=hook).sync(
        source, target, dry_run=dry_run
    )

    events = {event for event, *_ in seen}
    assert {
        SyncEvent.SyncStart,
        SyncEvent.CheckTargetChild,
        SyncEvent.CheckTargetChildren,
        SyncEvent.SyncChild,
        SyncEvent.SyncChildren,
    } <= events
    assert {reported for *_, reported in seen} == {dry_run}
    root_start = seen[0]
    assert root_start[0] is SyncEvent.SyncStart
    assert root_start[1] is not None and root_start[1].is_dir()
    assert root_start[2] is not None and root_start[2].is_dir()
    assert (target / "stale.txt").exists() is dry_run


def test_root_start_hook_reads_stat_without_attribute_error():
    source = MemPath("/src")
    source.mkdir()
    (source / "a.txt").write_text("abc")
    sizes = []

    PathSyncer(
        _size,
        hook=lambda s, t, e, dry: sizes.append(s.stat.st_size) if s.is_file() else None,
    ).sync(source / "a.txt", MemPath("/dst.txt"))

    assert sizes and set(sizes) == {3}


# --- sync-windows-dir-symlink-broken ------------------------------------------


def _dir_link_source(tmp_path):
    source = tmp_path / "src"
    (source / "z_real").mkdir(parents=True)
    (source / "z_real" / "f.txt").write_text("f")
    (source / "z_file.txt").write_text("x")
    # "a_" sorts first, so the link is created before its target exists at
    # the destination and os.symlink cannot guess the kind.
    _symlink("z_real", source / "a_dirlink", directory=True)
    _symlink("z_file.txt", source / "a_filelink")
    return source


def test_directory_symlink_is_created_as_directory_link(tmp_path, monkeypatch):
    source = _dir_link_source(tmp_path)
    target = tmp_path / "dst"
    calls = {}
    original = pathlib_next.LocalPath._symlink_to

    def recording(self, link_target, target_is_directory=False):
        calls[self.name] = target_is_directory
        return original(self, link_target, target_is_directory)

    monkeypatch.setattr(pathlib_next.LocalPath, "_symlink_to", recording)

    PathSyncer(_size, follow_symlinks=False).sync(
        pathlib_next.LocalPath(source), pathlib_next.LocalPath(target)
    )

    assert calls == {"a_dirlink": True, "a_filelink": False}
    assert sorted(os.listdir(target / "a_dirlink")) == ["f.txt"]
    assert (target / "a_filelink").read_text() == "x"


@pytest.mark.skipif(not IS_WINDOWS, reason="link kind is Windows-only")
def test_dangling_directory_link_keeps_its_kind(tmp_path):
    import stat as stat_mod

    source = tmp_path / "src"
    source.mkdir()
    _symlink("missing_dir", source / "dangling", directory=True)
    target = tmp_path / "dst"

    PathSyncer(_size, follow_symlinks=False).sync(
        pathlib_next.LocalPath(source), pathlib_next.LocalPath(target)
    )

    attributes = os.lstat(target / "dangling").st_file_attributes
    assert attributes & stat_mod.FILE_ATTRIBUTE_DIRECTORY


@pytest.mark.skipif(not IS_WINDOWS, reason="link kind is Windows-only")
def test_file_link_to_directory_from_earlier_run_is_repaired(tmp_path):
    source = _dir_link_source(tmp_path)
    target = tmp_path / "dst"
    target.mkdir()
    # What an earlier run left: same link text, wrong (file) kind.
    _symlink("z_real", target / "a_dirlink")

    PathSyncer(_size, follow_symlinks=False).sync(
        pathlib_next.LocalPath(source), pathlib_next.LocalPath(target)
    )

    assert sorted(os.listdir(target / "a_dirlink")) == ["f.txt"]


# --- sync-identical-symlink-recreated -------------------------------------------


def test_identical_symlink_is_not_recreated(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "real.txt").write_text("payload")
    _symlink("real.txt", source / "link")
    target = tmp_path / "dst"
    syncer = PathSyncer(_size, follow_symlinks=False)
    syncer.sync(pathlib_next.LocalPath(source), pathlib_next.LocalPath(target))
    before = os.lstat(target / "link")

    created = []
    monkeypatch.setattr(
        pathlib_next.LocalPath,
        "_symlink_to",
        lambda self, *args, **kwargs: created.append(self),
    )
    events, hook = _recorder()
    syncer._hook = hook
    syncer.sync(pathlib_next.LocalPath(source), pathlib_next.LocalPath(target))

    assert created == []
    assert SyncEvent.Symlink not in [e for _, _, e, _ in events]
    link_events = [e for s, _, e, _ in events if s.path.name == "link"]
    assert SyncEvent.Synced in link_events
    after = os.lstat(target / "link")
    assert (after.st_ino, after.st_ctime_ns) == (before.st_ino, before.st_ctime_ns)
    assert os.readlink(target / "link") == "real.txt"


def test_changed_symlink_is_still_replaced(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "new.txt").write_text("new")
    _symlink("new.txt", source / "link")
    target = tmp_path / "dst"
    target.mkdir()
    _symlink("old.txt", target / "link")
    events, hook = _recorder()

    PathSyncer(_size, follow_symlinks=False, hook=hook).sync(
        pathlib_next.LocalPath(source), pathlib_next.LocalPath(target)
    )

    assert pathlib_next.LocalPath(target / "link").readlink().as_posix() == "new.txt"
    assert SyncEvent.Symlink in [e for _, _, e, _ in events]


# --- sync-pathandstat-default-lstat ----------------------------------------------


def test_pathandstat_follows_symlinks_by_default(tmp_path):
    (tmp_path / "real.txt").write_text("x")
    _symlink("real.txt", tmp_path / "link")
    link = pathlib_next.LocalPath(tmp_path / "link")

    entry = PathAndStat(link)
    assert entry.is_file() is True
    assert entry.is_symlink() is False
    assert entry.is_file() == FileStat.from_path(link).is_file()

    entry.refresh()
    assert entry.is_file() is True

    lstat_entry = PathAndStat(link, follow_symlink=False)
    assert lstat_entry.is_symlink() is True
    lstat_entry.refresh(False)
    assert lstat_entry.is_symlink() is True
