"""PathSyncer must never destroy data outside what it was asked to mirror.

Regressions for the 2026-09-15 deep review (wave 1, phase 2). Every test
asserts that the data which must survive actually survives -- a raised
error alone proves nothing, the defects being fixed here raised nothing.
"""

import io
import os
import sys

import pytest
from capabilities import requires_fifos, requires_junctions

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.tools import uripath
from pathlib_next.utils.sync import PathSyncer, SyncEvent

IS_WINDOWS = sys.platform == "win32"


def _size(entry):
    return entry.stat.st_size


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _symlink(target, link, *, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")


def _collect():
    calls = []

    def ignore(error, source, target, event):
        calls.append((error, source, target, event))
        return True

    return calls, ignore


# --- sync-missing-root-source-wipes-target ---------------------------------


@pytest.mark.parametrize("remove_missing", [True, False])
def test_missing_root_source_raises_and_keeps_target(tmp_path, remove_missing):
    target = tmp_path / "important"
    _write(target / "keep" / "file.txt", "precious")
    syncer = PathSyncer(_size, remove_missing=remove_missing)

    with pytest.raises(FileNotFoundError):
        syncer.sync(
            pathlib_next.LocalPath(tmp_path / "typo_does_not_exist"),
            pathlib_next.LocalPath(target),
        )

    assert (target / "keep" / "file.txt").read_text() == "precious"


def test_missing_root_source_dry_run_raises(tmp_path):
    target = tmp_path / "important"
    _write(target / "file.txt", "precious")
    with pytest.raises(FileNotFoundError):
        PathSyncer(_size, remove_missing=True).sync(
            MemPath("/missing"), pathlib_next.LocalPath(target), dry_run=True
        )
    assert (target / "file.txt").read_text() == "precious"


def test_missing_root_source_ignored_error_changes_nothing(tmp_path):
    target = tmp_path / "important"
    _write(target / "file.txt", "precious")
    calls, ignore = _collect()

    PathSyncer(_size, remove_missing=True, ignore_error=ignore).sync(
        MemPath("/missing"), pathlib_next.LocalPath(target)
    )

    assert (target / "file.txt").read_text() == "precious"
    assert len(calls) == 1
    error, source, _, event = calls[0]
    assert isinstance(error, FileNotFoundError)
    assert event is SyncEvent.SyncStart
    assert source.path == MemPath("/missing")


def test_missing_root_source_cli_keeps_target(tmp_path):
    target = tmp_path / "backup"
    _write(target / "file.txt", "precious")
    stderr = io.StringIO()
    code = uripath.main(
        ["sync", "--remove-missing", str(tmp_path / "buidl"), str(target)],
        stderr=stderr,
    )
    assert code == 1
    assert "FileNotFoundError" in stderr.getvalue()
    assert (target / "file.txt").read_text() == "precious"


def test_vanished_child_is_still_removed(tmp_path):
    # Only the ROOT is guarded: a child that is listed but gone by the time
    # it is synced keeps the RemovedMissing behaviour.
    class GhostMemPath(MemPath):
        def _scandir(self):
            yield from super()._scandir()
            if self.as_posix() == "/src":
                yield "ghost.txt", None

    source = GhostMemPath("/src")
    source.mkdir()
    (source / "a.txt").write_text("aaa")
    target = tmp_path / "dst"
    _write(target / "ghost.txt", "stale")
    _write(target / "a.txt", "aaa")

    PathSyncer(_size, remove_missing=True).sync(source, pathlib_next.LocalPath(target))

    assert not (target / "ghost.txt").exists()
    assert (target / "a.txt").read_text() == "aaa"


# --- sync-child-name-escapes-destination -----------------------------------


def _listing_mempath(extra_names):
    class ListingMemPath(MemPath):
        def _scandir(self):
            yield from super()._scandir()
            if self.as_posix() == "/src":
                for name in extra_names:
                    yield name, None

    return ListingMemPath


def _escape_layout(tmp_path):
    mirror = tmp_path / "mirror"
    dst = mirror / "dst"
    dst.mkdir(parents=True)
    _write(mirror / "precious.txt", "precious")
    _write(tmp_path / "sibling" / "keep.txt", "keep")
    return mirror, dst


def _assert_nothing_escaped(tmp_path, mirror):
    assert (mirror / "precious.txt").read_text() == "precious"
    assert (tmp_path / "sibling" / "keep.txt").read_text() == "keep"
    assert sorted(p.name for p in mirror.iterdir()) == ["dst", "precious.txt"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mirror", "sibling"]


@pytest.mark.parametrize("remove_missing", [True, False])
def test_dotdot_child_name_raises_and_parent_survives(tmp_path, remove_missing):
    cls = _listing_mempath([".."])
    source = cls("/src")
    source.mkdir()
    (source / "ok.txt").write_text("ok")
    mirror, dst = _escape_layout(tmp_path)

    with pytest.raises(ValueError):
        PathSyncer(_size, remove_missing=remove_missing).sync(
            source, pathlib_next.LocalPath(dst)
        )

    _assert_nothing_escaped(tmp_path, mirror)


def test_name_fallback_producing_dotdot_is_rejected(tmp_path):
    # "../" gives a child path whose `.name` is empty; the parent-name
    # fallback used for URI directory children then yields "..".
    cls = _listing_mempath(["../"])
    source = cls("/src")
    source.mkdir()
    (source / "ok.txt").write_text("ok")
    mirror, dst = _escape_layout(tmp_path)
    calls, ignore = _collect()

    PathSyncer(_size, remove_missing=True, ignore_error=ignore).sync(
        source, pathlib_next.LocalPath(dst)
    )

    _assert_nothing_escaped(tmp_path, mirror)
    assert (dst / "ok.txt").read_text() == "ok"
    rejected = [c for c in calls if isinstance(c[0], ValueError)]
    assert len(rejected) == 1
    _, source_entry, target_entry, event = rejected[0]
    assert event is SyncEvent.SyncChild
    # MemPath normalizes like PurePosixPath, so the listed "../" is "/src/..".
    assert source_entry.path.as_posix() in ("/src/../", "/src/..")
    assert target_entry.path == pathlib_next.LocalPath(dst)


@pytest.mark.skipif(not IS_WINDOWS, reason="backslash/drive only split on Windows")
@pytest.mark.parametrize("name", ["..\\..\\escaped.txt", "D:escaped.txt", "a:b"])
def test_windows_unsafe_child_name_never_lands_outside(tmp_path, name):
    source = MemPath("/src")
    source.mkdir()
    (source / name).write_text("evil")
    (source / "ok.txt").write_text("ok")
    mirror, dst = _escape_layout(tmp_path)
    calls, ignore = _collect()

    PathSyncer(_size, ignore_error=ignore).sync(source, pathlib_next.LocalPath(dst))

    _assert_nothing_escaped(tmp_path, mirror)
    assert sorted(p.name for p in dst.iterdir()) == ["ok.txt"]
    assert [type(c[0]) for c in calls] == [ValueError]


@pytest.mark.skipif(not IS_WINDOWS, reason="backslash/drive only split on Windows")
def test_windows_unsafe_child_name_default_policy_raises(tmp_path):
    source = MemPath("/src")
    source.mkdir()
    (source / "..\\escaped.txt").write_text("evil")
    mirror, dst = _escape_layout(tmp_path)

    with pytest.raises(ValueError):
        PathSyncer(_size).sync(source, pathlib_next.LocalPath(dst))

    _assert_nothing_escaped(tmp_path, mirror)


def test_backslash_name_is_legal_on_a_posix_flavoured_target():
    # MemPath treats "\\" as an ordinary character: nothing to reject.
    source = MemPath("/src")
    source.mkdir()
    (source / "a\\b.txt").write_text("x")
    target = MemPath("/dst")

    PathSyncer(_size).sync(source, target)

    assert (target / "a\\b.txt").read_text() == "x"


def test_unsafe_target_listing_name_is_never_removed():
    # remove_missing joins TARGET listing names too; a ".." there must not
    # turn into rm(recursive=True) of the destination's parent.
    class DotDotTarget(MemPath):
        def _scandir(self):
            yield from super()._scandir()
            if self.as_posix() == "/mirror/dst":
                yield "..", None

    source = MemPath("/src")
    source.mkdir()
    (source / "ok.txt").write_text("ok")
    target = DotDotTarget("/mirror/dst")
    target.mkdir(parents=True)
    (target.parent / "precious.txt").write_text("precious")

    with pytest.raises(ValueError):
        PathSyncer(_size, remove_missing=True).sync(source, target)

    assert (target.parent / "precious.txt").read_text() == "precious"
    assert target.is_dir()


# --- sync-unknown-stat-treated-as-missing ----------------------------------


class _NoStatMemPath(MemPath):
    """Lists like GitLab: every entry's stat is unknown (None)."""

    def _scandir(self):
        for name, _ in super()._scandir():
            yield name, None


@pytest.mark.parametrize("remove_missing", [True, False])
def test_unknown_listing_stat_is_restatted_not_missing(tmp_path, remove_missing):
    source = _NoStatMemPath("/")
    (source / "doc.txt").write_text("doc")
    (source / "new.txt").write_text("new")
    (source / "sub").mkdir()
    (source / "sub" / "deep.txt").write_text("deep")
    target = tmp_path / "mirror"
    _write(target / "doc.txt", "doc")

    PathSyncer(_size, follow_symlinks=False, remove_missing=remove_missing).sync(
        source, pathlib_next.LocalPath(target)
    )

    assert (target / "doc.txt").read_text() == "doc"
    assert (target / "new.txt").read_text() == "new"
    assert (target / "sub" / "deep.txt").read_text() == "deep"


# --- sync-target-symlinks-escape-tree (mechanism a) ------------------------


@pytest.mark.parametrize("follow_symlinks", [False, True])
def test_target_dir_symlink_is_replaced_not_written_through(tmp_path, follow_symlinks):
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "unrelated.txt", "unrelated")
    src = tmp_path / "src"
    _write(src / "conf" / "app.ini", "ini")
    dst = tmp_path / "dst"
    dst.mkdir()
    _symlink(elsewhere, dst / "conf", directory=True)

    PathSyncer(_size, follow_symlinks=follow_symlinks, remove_missing=True).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst)
    )

    assert (elsewhere / "unrelated.txt").read_text() == "unrelated"
    assert sorted(p.name for p in elsewhere.iterdir()) == ["unrelated.txt"]
    assert not (dst / "conf").is_symlink()
    assert (dst / "conf" / "app.ini").read_text() == "ini"


def test_preserve_then_real_dir_second_run_keeps_outside_data(tmp_path):
    # The documented flow: run 1 mirrors a link, the source then replaces
    # it with a real directory, run 2 must not act through the old link.
    shared = tmp_path / "shared"
    _write(shared / "team.ini", "team")
    src = tmp_path / "src"
    src.mkdir()
    _symlink(shared, src / "conf", directory=True)
    dst = tmp_path / "dst"
    syncer = PathSyncer(_size, follow_symlinks=False, remove_missing=True)
    syncer.sync(pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst))
    assert (dst / "conf").is_symlink()

    (src / "conf").unlink()
    _write(src / "conf" / "app.ini", "ini")
    syncer.sync(pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst))

    assert (shared / "team.ini").read_text() == "team"
    assert sorted(p.name for p in shared.iterdir()) == ["team.ini"]
    assert not (dst / "conf").is_symlink()
    assert (dst / "conf" / "app.ini").read_text() == "ini"


@pytest.mark.parametrize("follow_symlinks", [False, True])
def test_target_dangling_file_symlink_is_not_written_through(tmp_path, follow_symlinks):
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src"
    _write(src / "a.txt", "content")
    dst = tmp_path / "dst"
    dst.mkdir()
    _symlink(outside / "planted.txt", dst / "a.txt")

    PathSyncer(_size, follow_symlinks=follow_symlinks).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst)
    )

    assert list(outside.iterdir()) == []
    assert not (dst / "a.txt").is_symlink()
    assert (dst / "a.txt").read_text() == "content"


@pytest.mark.parametrize("follow_symlinks", [False, True])
def test_root_target_symlink_is_followed(tmp_path, follow_symlinks):
    # The destination the caller named may itself be a link (a backup
    # directory on another disk): it is used, not replaced.
    real = tmp_path / "real_dst"
    _write(real / "old.txt", "old")
    link = tmp_path / "dst_link"
    _symlink(real, link, directory=True)
    src = tmp_path / "src"
    _write(src / "a.txt", "aaa")

    PathSyncer(_size, follow_symlinks=follow_symlinks).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(link)
    )

    assert link.is_symlink()
    assert (real / "a.txt").read_text() == "aaa"
    assert (real / "old.txt").read_text() == "old"


# --- sync-overlapping-source-target ----------------------------------------


def test_source_inside_target_raises_and_source_survives(tmp_path):
    data = tmp_path / "data"
    _write(data / "src" / "only_copy.txt", "the only copy")

    with pytest.raises(ValueError):
        PathSyncer(_size, remove_missing=True).sync(
            pathlib_next.LocalPath(data / "src"), pathlib_next.LocalPath(data)
        )

    assert (data / "src" / "only_copy.txt").read_text() == "the only copy"
    assert sorted(p.name for p in data.iterdir()) == ["src"]


def test_target_inside_source_raises_without_nesting():
    root = MemPath("/")
    (root / "a.txt").write_text("aaa")
    calls, ignore = _collect()

    PathSyncer(_size, ignore_error=ignore).sync(root, root / "backup")

    assert not (root / "backup").exists()
    assert (root / "a.txt").read_text() == "aaa"
    assert [type(c[0]) for c in calls] == [ValueError]

    with pytest.raises(ValueError):
        PathSyncer(_size).sync(root, root / "backup")
    assert not (root / "backup").exists()


def test_same_path_raises_and_tree_survives():
    root = MemPath("/tree")
    root.mkdir()
    (root / "a.txt").write_text("aaa")
    same = MemPath("/tree", backend=root.backend)
    with pytest.raises(ValueError):
        PathSyncer(_size, remove_missing=True).sync(root, same)
    assert (root / "a.txt").read_text() == "aaa"


def test_equal_mempath_segments_on_different_backends_do_not_overlap():
    source = MemPath("/tree")
    source.mkdir()
    (source / "a.txt").write_text("aaa")
    target = MemPath("/tree")  # fresh, separate backend
    assert source == target  # equality ignores the backend

    PathSyncer(_size).sync(source, target)

    assert (target / "a.txt").read_text() == "aaa"
    assert (source / "a.txt").read_text() == "aaa"


def test_overlap_hidden_behind_symlink_is_detected(tmp_path):
    data = tmp_path / "data"
    _write(data / "src" / "only_copy.txt", "the only copy")
    link = tmp_path / "data_link"
    _symlink(data, link, directory=True)

    with pytest.raises(ValueError):
        PathSyncer(_size, remove_missing=True).sync(
            pathlib_next.LocalPath(data / "src"), pathlib_next.LocalPath(link)
        )

    assert (data / "src" / "only_copy.txt").read_text() == "the only copy"


def test_sibling_prefix_is_not_an_overlap(tmp_path):
    # "data2" merely shares a string prefix with "data".
    _write(tmp_path / "data" / "a.txt", "aaa")

    PathSyncer(_size).sync(
        pathlib_next.LocalPath(tmp_path / "data"),
        pathlib_next.LocalPath(tmp_path / "data2"),
    )

    assert (tmp_path / "data2" / "a.txt").read_text() == "aaa"
    assert (tmp_path / "data" / "a.txt").read_text() == "aaa"


# --- wave 5 (G6): remaining PathSyncer soundness findings ------------------

import errno
import stat as stat_module

from pathlib_next.utils.stat import FileStat


def _events():
    events = []

    def hook(source, target, event, dry_run):
        source = source.path
        target = target.path
        events.append((event, str(source), str(target), dry_run))

    return events, hook


# sync-symlink-preserve-deletes-before-failing


def test_preserve_onto_backend_without_symlinks_keeps_existing_entry(tmp_path):
    real = tmp_path / "real"
    _write(real / "file.txt", "content")
    source = tmp_path / "src"
    source.mkdir()
    _symlink(real, source / "data", directory=True)
    target = MemPath("/dst")
    (target / "data").mkdir(parents=True)
    (target / "data" / "precious.txt").write_text("precious")
    calls, ignore = _collect()

    PathSyncer(_size, follow_symlinks=False, ignore_error=ignore).sync(
        pathlib_next.LocalPath(source), target
    )

    assert (target / "data" / "precious.txt").read_text() == "precious"
    assert len(calls) == 1
    error, _, failing_target, event = calls[0]
    assert isinstance(error, NotImplementedError)
    assert event is SyncEvent.Symlink
    assert failing_target.path == target / "data"


def test_preserve_runtime_symlink_refusal_keeps_existing_file(tmp_path, monkeypatch):
    # A backend that implements symlinks but refuses at runtime (WinError
    # 1314 without the privilege, an SFTP permission error): the refusal
    # happens on a temporary sibling, before the target is removed.
    source = tmp_path / "src"
    source.mkdir()
    _write(tmp_path / "real.txt", "real")
    _symlink(tmp_path / "real.txt", source / "link.txt")
    target = tmp_path / "dst"
    _write(target / "link.txt", "previous regular file")

    def refuse(self, target, target_is_directory=False):
        raise PermissionError(errno.EPERM, "no symlink privilege", str(self))

    monkeypatch.setattr(pathlib_next.LocalPath, "_symlink_to", refuse)
    with pytest.raises(PermissionError):
        PathSyncer(_size, follow_symlinks=False).sync(
            pathlib_next.LocalPath(source), pathlib_next.LocalPath(target)
        )

    assert (target / "link.txt").read_text() == "previous regular file"
    assert sorted(p.name for p in target.iterdir()) == ["link.txt"]


def test_preserve_replaces_existing_file_with_link(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    _write(tmp_path / "real.txt", "real")
    _symlink(os.path.join("..", "real.txt"), source / "link.txt")
    target = tmp_path / "dst"
    _write(target / "link.txt", "old")

    PathSyncer(_size, follow_symlinks=False).sync(
        pathlib_next.LocalPath(source), pathlib_next.LocalPath(target)
    )

    assert (target / "link.txt").is_symlink()
    assert os.readlink(target / "link.txt") == os.path.join("..", "real.txt")
    assert sorted(p.name for p in target.iterdir()) == ["link.txt"]


# sync-copy-not-atomic


class _ResetStream(io.RawIOBase):
    """Serves one chunk, then fails like a dropped connection."""

    def __init__(self, data):
        self._data = data
        self._reads = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        self._reads += 1
        if self._reads > 1:
            raise ConnectionResetError(errno.ECONNRESET, "connection reset")
        chunk = self._data[:4]
        buffer[: len(chunk)] = chunk
        return len(chunk)


class _FlakyMemPath(MemPath):
    def _open(self, mode="r", buffering=-1):
        handle = super()._open(mode, buffering)
        if mode == "r" and self.name == "data.bin":
            return _ResetStream(handle.read())
        return handle


def _flaky_source():
    source = _FlakyMemPath("/src")
    source.mkdir()
    (source / "data.bin").write_bytes(b"NEW! content of a different size")
    return source


def test_failed_copy_keeps_previous_local_version(tmp_path):
    target = tmp_path / "dst"
    _write(target / "data.bin", "previous good version")

    with pytest.raises(ConnectionResetError):
        PathSyncer(_size).sync(_flaky_source(), pathlib_next.LocalPath(target))

    assert (target / "data.bin").read_text() == "previous good version"
    # The temporary sibling is gone too.
    assert sorted(p.name for p in target.iterdir()) == ["data.bin"]


def test_failed_copy_ignored_keeps_previous_version_and_continues(tmp_path):
    source = _flaky_source()
    (source / "z.txt").write_text("later sibling")
    target = tmp_path / "dst"
    _write(target / "data.bin", "previous good version")
    calls, ignore = _collect()

    PathSyncer(_size, ignore_error=ignore).sync(source, pathlib_next.LocalPath(target))

    assert (target / "data.bin").read_text() == "previous good version"
    assert (target / "z.txt").read_text() == "later sibling"
    assert [event for _, _, _, event in calls] == [SyncEvent.Copy]


def test_copy_replaces_changed_local_file(tmp_path):
    source = MemPath("/src")
    source.mkdir()
    (source / "a.txt").write_text("new, longer content")
    target = tmp_path / "dst"
    _write(target / "a.txt", "old")

    PathSyncer(_size).sync(source, pathlib_next.LocalPath(target))

    assert (target / "a.txt").read_text() == "new, longer content"
    assert sorted(p.name for p in target.iterdir()) == ["a.txt"]


def test_failed_copy_onto_backend_without_rename_keeps_target():
    # MemPath has no rename(): overwritten in place, but the source is
    # opened before the target is truncated.
    class VanishingMemPath(MemPath):
        def _open(self, mode="r", buffering=-1):
            if mode == "r" and self.name == "a.txt":
                raise FileNotFoundError(errno.ENOENT, "vanished", str(self))
            return super()._open(mode, buffering)

    source = VanishingMemPath("/src")
    source.mkdir()
    (source / "a.txt").write_text("a different size")
    target = MemPath("/dst")
    target.mkdir()
    (target / "a.txt").write_text("previous")

    with pytest.raises(FileNotFoundError):
        PathSyncer(_size).sync(source, target)

    assert (target / "a.txt").read_text() == "previous"


# sync-dry-run-crashes


def _mutations(events):
    mutating = {
        SyncEvent.Copy,
        SyncEvent.RemovedMissing,
        SyncEvent.CreatedDirectory,
        SyncEvent.TypeMismatch,
        SyncEvent.Symlink,
    }
    return [(e, s, t) for e, s, t, _ in events if e in mutating]


def test_dry_run_over_new_subdirectory_with_remove_missing(tmp_path):
    source = tmp_path / "src"
    _write(source / "newsub" / "deep" / "f.txt", "f")
    _write(source / "keep.txt", "keep")
    target = tmp_path / "dst"
    _write(target / "keep.txt", "keep")
    _write(target / "stale.txt", "stale")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

    dry_events, dry_hook = _events()
    PathSyncer(_size, remove_missing=True, hook=dry_hook).sync(
        pathlib_next.LocalPath(source), pathlib_next.LocalPath(target), dry_run=True
    )

    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before

    real_events, real_hook = _events()
    PathSyncer(_size, remove_missing=True, hook=real_hook).sync(
        pathlib_next.LocalPath(source), pathlib_next.LocalPath(target)
    )
    assert _mutations(dry_events) == _mutations(real_events)
    assert (target / "newsub" / "deep" / "f.txt").read_text() == "f"
    assert not (target / "stale.txt").exists()


def test_dry_run_onto_absent_root_with_remove_missing(tmp_path):
    source = tmp_path / "src"
    _write(source / "sub" / "f.txt", "f")
    target = tmp_path / "dst"

    PathSyncer(_size, remove_missing=True).sync(
        pathlib_next.LocalPath(source), pathlib_next.LocalPath(target), dry_run=True
    )

    assert not target.exists()


@pytest.mark.parametrize("remove_missing", [True, False])
def test_dry_run_over_file_to_directory_change(remove_missing):
    source = MemPath("/src")
    (source / "x").mkdir(parents=True)
    (source / "x" / "child.txt").write_text("child")
    target = MemPath("/dst")
    target.mkdir()
    (target / "x").write_text("was a file")

    dry_events, dry_hook = _events()
    PathSyncer(_size, remove_missing=remove_missing, hook=dry_hook).sync(
        source, target, dry_run=True
    )

    assert (target / "x").read_text() == "was a file"

    real_events, real_hook = _events()
    PathSyncer(_size, remove_missing=remove_missing, hook=real_hook).sync(
        source, target
    )
    assert _mutations(dry_events) == _mutations(real_events)
    assert (target / "x" / "child.txt").read_text() == "child"


# sync-removedmissing-event-reports-parent


def test_removed_missing_event_names_the_removed_entry(tmp_path):
    source = MemPath("/src")
    (source / "sub").mkdir(parents=True)
    target = tmp_path / "dst"
    _write(target / "stale1.txt", "1")
    _write(target / "sub" / "stale2.txt", "2")
    events, hook = _events()

    PathSyncer(_size, remove_missing=True, hook=hook).sync(
        source, pathlib_next.LocalPath(target), dry_run=True
    )

    removed = sorted(t for e, _, t, _ in events if e is SyncEvent.RemovedMissing)
    assert removed == sorted(
        [str(target / "stale1.txt"), str(target / "sub" / "stale2.txt")]
    )
    sources = sorted(s for e, s, _, _ in events if e is SyncEvent.RemovedMissing)
    assert sources == ["/src/stale1.txt", "/src/sub/stale2.txt"]
    assert (target / "stale1.txt").exists()


# sync-error-policy-duplicated-and-misattributed


def _nested_trees(tmp_path):
    source = MemPath("/src")
    (source / "l1" / "l2").mkdir(parents=True)
    (source / "l1" / "l2" / "f.txt").write_text("source")
    target = tmp_path / "dst"
    _write(target / "l1" / "l2" / "f.txt", "target")
    return source, pathlib_next.LocalPath(target)


def test_checksum_error_reaches_policy_once_with_the_file(tmp_path):
    def bad_checksum(entry):
        raise RuntimeError("checksum failed")

    source, target = _nested_trees(tmp_path)
    calls = []

    def decline(error, source_entry, target_entry, event):
        calls.append((error, str(source_entry.path), target_entry.path, event))
        return False

    with pytest.raises(RuntimeError):
        PathSyncer(bad_checksum, ignore_error=decline).sync(source, target)

    assert len(calls) == 1
    _, source_path, target_path, event = calls[0]
    assert source_path == "/src/l1/l2/f.txt"
    assert target_path == target / "l1" / "l2" / "f.txt"
    assert event is SyncEvent.Compare


def test_copy_error_reaches_policy_once(tmp_path):
    target = tmp_path / "dst"
    _write(target / "data.bin", "previous good version")
    calls = []

    def decline(error, source_entry, target_entry, event):
        calls.append((str(target_entry.path), event))
        return False

    with pytest.raises(ConnectionResetError):
        PathSyncer(_size, ignore_error=decline).sync(
            _flaky_source(), pathlib_next.LocalPath(target)
        )

    assert calls == [(str(target / "data.bin"), SyncEvent.Copy)]


def test_root_file_pair_checksum_error_reaches_policy(tmp_path):
    def bad_checksum(entry):
        raise RuntimeError("checksum failed")

    _write(tmp_path / "a.txt", "a")
    _write(tmp_path / "b.txt", "b")
    calls, ignore = _collect()

    PathSyncer(bad_checksum, ignore_error=ignore).sync(
        pathlib_next.LocalPath(tmp_path / "a.txt"),
        pathlib_next.LocalPath(tmp_path / "b.txt"),
    )

    assert [event for _, _, _, event in calls] == [SyncEvent.Compare]
    assert (tmp_path / "b.txt").read_text() == "b"

    with pytest.raises(RuntimeError):
        PathSyncer(bad_checksum).sync(
            pathlib_next.LocalPath(tmp_path / "a.txt"),
            pathlib_next.LocalPath(tmp_path / "b.txt"),
        )


def test_tolerated_compare_error_does_not_stop_siblings(tmp_path):
    def checksum(entry):
        if entry.path.name == "bad.txt":
            raise RuntimeError("checksum failed")
        return entry.stat.st_size

    source = MemPath("/src")
    source.mkdir()
    (source / "bad.txt").write_text("source")
    (source / "good.txt").write_text("new content")
    target = tmp_path / "dst"
    _write(target / "bad.txt", "target")
    _write(target / "good.txt", "old")
    calls, ignore = _collect()

    PathSyncer(checksum, ignore_error=ignore).sync(
        source, pathlib_next.LocalPath(target)
    )

    assert len(calls) == 1
    assert (target / "bad.txt").read_text() == "target"
    assert (target / "good.txt").read_text() == "new content"


# sync-special-files-treated-as-directories


class _FifoMemPath(MemPath):
    """Reports the entry named "fifo" as a named pipe."""

    def stat(self, *, follow_symlinks=True):
        result = super().stat(follow_symlinks=follow_symlinks)
        if self.name == "fifo":
            return FileStat(st_mode=stat_module.S_IFIFO | 0o644)
        return result

    def _scandir(self):
        for name, _ in super()._scandir():
            yield name, None


@pytest.mark.parametrize("follow_symlinks", [True, False])
def test_special_source_entry_is_skipped_not_made_a_directory(
    tmp_path, follow_symlinks
):
    source = _FifoMemPath("/src")
    source.mkdir()
    (source / "fifo").write_text("")
    (source / "z.txt").write_text("later sibling")
    target = tmp_path / "dst"
    _write(target / "fifo", "a regular target file")
    events, hook = _events()

    PathSyncer(
        _size, remove_missing=True, follow_symlinks=follow_symlinks, hook=hook
    ).sync(source, pathlib_next.LocalPath(target))

    assert (target / "fifo").read_text() == "a regular target file"
    assert (target / "z.txt").read_text() == "later sibling"
    fifo_events = [e for e, s, _, _ in events if s == "/src/fifo"]
    assert SyncEvent.Skipped in fifo_events
    assert SyncEvent.CreatedDirectory not in fifo_events
    assert SyncEvent.TypeMismatch not in fifo_events


@requires_fifos
def test_real_fifo_is_skipped(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    os.mkfifo(source / "pipe")
    _write(source / "z.txt", "z")
    target = tmp_path / "dst"
    _write(target / "pipe", "keep me")

    PathSyncer(_size).sync(
        pathlib_next.LocalPath(source), pathlib_next.LocalPath(target)
    )

    assert (target / "pipe").read_text() == "keep me"
    assert (target / "z.txt").read_text() == "z"


# --- a listed entry is never "missing" ---------------------------------------

from pathlib_next.utils.sync import PathAndStat


def _dangling_layout(tmp_path):
    """A source whose `data` and `file.txt` are links to nothing (an
    unmounted volume), over a target that holds the real thing."""
    src = tmp_path / "src"
    _write(src / "keep.txt", "keep")
    _symlink(tmp_path / "not-mounted", src / "data", directory=True)
    _symlink(tmp_path / "gone.txt", src / "file.txt")
    dst = tmp_path / "dst"
    _write(dst / "data" / "precious.txt", "precious")
    _write(dst / "file.txt", "precious too")
    return src, dst


@pytest.mark.parametrize("remove_missing", [True, False])
@pytest.mark.parametrize("dry_run", [True, False])
def test_dangling_source_link_is_an_error_not_a_removal(
    tmp_path, remove_missing, dry_run
):
    src, dst = _dangling_layout(tmp_path)
    calls, ignore = _collect()

    PathSyncer(_size, remove_missing=remove_missing, ignore_error=ignore).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst), dry_run=dry_run
    )

    assert (dst / "data" / "precious.txt").read_text() == "precious"
    assert (dst / "file.txt").read_text() == "precious too"
    assert (dst / "keep.txt").exists() is not dry_run
    reported = {c[1].path.name: c for c in calls}
    assert sorted(reported) == ["data", "file.txt"]
    for error, source, target, event in reported.values():
        assert isinstance(error, FileNotFoundError)
        assert isinstance(source, PathAndStat) and source.is_symlink()
        assert isinstance(target, PathAndStat)
        assert target.path.name == source.path.name
        assert event is SyncEvent.SyncStart


@pytest.mark.parametrize("remove_missing", [True, False])
def test_dangling_source_link_raises_by_default_and_keeps_target(
    tmp_path, remove_missing
):
    src, dst = _dangling_layout(tmp_path)

    with pytest.raises(FileNotFoundError):
        PathSyncer(_size, remove_missing=remove_missing).sync(
            pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst)
        )

    assert (dst / "data" / "precious.txt").read_text() == "precious"
    assert (dst / "file.txt").read_text() == "precious too"


def test_source_link_loop_is_an_error_for_that_entry_only(tmp_path):
    src = tmp_path / "src"
    _write(src / "keep.txt", "keep")
    _symlink(src / "b", src / "a")
    _symlink(src / "a", src / "b")
    dst = tmp_path / "dst"
    _write(dst / "a", "precious a")
    _write(dst / "b", "precious b")
    calls, ignore = _collect()

    PathSyncer(_size, remove_missing=True, ignore_error=ignore).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst)
    )

    assert (dst / "a").read_text() == "precious a"
    assert (dst / "b").read_text() == "precious b"
    assert (dst / "keep.txt").read_text() == "keep"
    assert sorted(c[1].path.name for c in calls) == ["a", "b"]
    assert all(isinstance(c[0], OSError) for c in calls)


def test_dangling_link_is_synced_as_a_link_when_links_are_not_followed(tmp_path):
    src, dst = _dangling_layout(tmp_path)

    PathSyncer(_size, follow_symlinks=False, remove_missing=True).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst)
    )

    assert (dst / "data").is_symlink() and (dst / "file.txt").is_symlink()


class _ScanMemPath(MemPath):
    """Lists like a stdlib scandir: entries with a `stat()` method."""

    def _scandir(self):
        class Entry:
            def __init__(self, name, path):
                self.name, self._path = name, path

            def stat(self, *, follow_symlinks=True):
                return self._path.stat(follow_symlinks=follow_symlinks)

        for name, _ in super()._scandir():
            yield Entry(name, self / name)


def test_listing_entries_with_a_stat_method_are_synced():
    source = _ScanMemPath("/src")
    source.mkdir()
    (source / "a.txt").write_text("aaa")
    (source / "sub").mkdir()
    (source / "sub" / "b.txt").write_text("bb")
    target = MemPath("/dst")

    PathSyncer(_size).sync(source, target)

    assert (target / "a.txt").read_text() == "aaa"
    assert (target / "sub" / "b.txt").read_text() == "bb"


# --- the error policy receives the public entry type ------------------------


class _StatDenied(MemPath):
    def stat(self, *, follow_symlinks=True):
        raise PermissionError(errno.EACCES, "denied", str(self))


@pytest.mark.parametrize("denied", ["source", "target"])
def test_policy_receives_entries_when_the_root_stat_fails(denied):
    seen = []

    def policy(error, source, target, event):
        seen.append((error, source, target, event))
        return True

    backend = MemPath("/").backend
    healthy = MemPath("/ok", backend=backend)
    broken = _StatDenied("/denied", backend=backend)
    pair = (broken, healthy) if denied == "source" else (healthy, broken)

    PathSyncer(_size, ignore_error=policy).sync(*pair)

    assert len(seen) == 1
    error, source, target, event = seen[0]
    assert isinstance(error, PermissionError)
    assert event is SyncEvent.SyncStart
    assert isinstance(source, PathAndStat) and isinstance(target, PathAndStat)
    assert (source if denied == "source" else target).path == broken
    assert (source if denied == "source" else target).stat is None


# --- a binding inside the target is replaced, never listed or written through


def _junction(target, link):
    """A Windows junction at `link` naming `target`, both under tmp_path."""
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


@requires_junctions
@pytest.mark.parametrize("remove_missing", [True, False])
def test_junction_in_target_is_replaced_and_what_it_names_is_untouched(
    tmp_path, remove_missing
):
    outside = tmp_path / "outside"
    _write(outside / "unrelated.txt", "unrelated")
    src = tmp_path / "src"
    _write(src / "sub" / "new.txt", "new")
    dst = tmp_path / "dst"
    dst.mkdir()
    _junction(outside, dst / "sub")
    events, hook = _events()

    PathSyncer(_size, remove_missing=remove_missing, hook=hook).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst)
    )

    assert (outside / "unrelated.txt").read_text() == "unrelated"
    assert sorted(p.name for p in outside.iterdir()) == ["unrelated.txt"]
    assert not pathlib_next.LocalPath(dst / "sub").is_junction()
    assert (dst / "sub" / "new.txt").read_text() == "new"
    mismatches = [t for e, _, t, _ in events if e is SyncEvent.TypeMismatch]
    assert mismatches == [str(dst / "sub")]


@requires_junctions
def test_dry_run_leaves_a_junction_in_target_and_reports_its_replacement(tmp_path):
    outside = tmp_path / "outside"
    _write(outside / "unrelated.txt", "unrelated")
    src = tmp_path / "src"
    _write(src / "sub" / "new.txt", "new")
    dst = tmp_path / "dst"
    dst.mkdir()
    _junction(outside, dst / "sub")
    dry_events, dry_hook = _events()
    real_events, real_hook = _events()

    PathSyncer(_size, remove_missing=True, hook=dry_hook).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst), dry_run=True
    )

    assert pathlib_next.LocalPath(dst / "sub").is_junction()
    assert sorted(p.name for p in outside.iterdir()) == ["unrelated.txt"]
    PathSyncer(_size, remove_missing=True, hook=real_hook).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst)
    )
    assert _mutations(dry_events) == _mutations(real_events)
    assert sorted(p.name for p in outside.iterdir()) == ["unrelated.txt"]


@requires_junctions
def test_junction_missing_from_source_is_removed_without_emptying_what_it_names(
    tmp_path,
):
    outside = tmp_path / "outside"
    _write(outside / "unrelated.txt", "unrelated")
    src = tmp_path / "src"
    _write(src / "keep.txt", "keep")
    dst = tmp_path / "dst"
    dst.mkdir()
    _junction(outside, dst / "gone")

    PathSyncer(_size, remove_missing=True).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst)
    )

    assert not (dst / "gone").exists()
    assert (outside / "unrelated.txt").read_text() == "unrelated"


@pytest.fixture
def mount_named_mounted(monkeypatch):
    """`is_mount()` answers True for a directory called `mounted`; creating a
    real mount point needs privileges."""
    real_is_mount = pathlib_next.LocalPath.is_mount

    def fake_is_mount(self):
        return self.name == "mounted" or real_is_mount(self)

    monkeypatch.setattr(pathlib_next.LocalPath, "is_mount", fake_is_mount)


def test_mount_point_in_target_is_part_of_the_tree(tmp_path, mount_named_mounted):
    # A mounted volume below the target is a filesystem boundary, not a link:
    # the sync writes into it and keeps what the source does not name.
    src = tmp_path / "src"
    _write(src / "mounted" / "new.txt", "new")
    _write(src / "own.txt", "own")
    dst = tmp_path / "dst"
    _write(dst / "mounted" / "theirs.txt", "theirs")
    events, hook = _events()

    PathSyncer(_size, hook=hook).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst)
    )

    assert (dst / "mounted" / "new.txt").read_text() == "new"
    assert (dst / "mounted" / "theirs.txt").read_text() == "theirs"
    assert (dst / "own.txt").read_text() == "own"
    assert not [e for e, _, _, _ in events if e is SyncEvent.TypeMismatch]


def test_mount_point_in_target_follows_remove_missing_like_a_directory(
    tmp_path, mount_named_mounted
):
    src = tmp_path / "src"
    _write(src / "mounted" / "new.txt", "new")
    dst = tmp_path / "dst"
    _write(dst / "mounted" / "stale.txt", "stale")

    PathSyncer(_size, remove_missing=True).sync(
        pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst)
    )

    assert sorted(p.name for p in (dst / "mounted").iterdir()) == ["new.txt"]


@requires_junctions
def test_junction_that_is_a_mount_point_is_synced_into(tmp_path, mount_named_mounted):
    # A mounted volume is built from the same reparse point as a junction.
    outside = tmp_path / "volume"
    _write(outside / "theirs.txt", "theirs")
    src = tmp_path / "src"
    _write(src / "mounted" / "new.txt", "new")
    dst = tmp_path / "dst"
    dst.mkdir()
    _junction(outside, dst / "mounted")

    PathSyncer(_size).sync(pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst))

    assert pathlib_next.LocalPath(dst / "mounted").is_junction()
    assert (outside / "new.txt").read_text() == "new"
    assert (outside / "theirs.txt").read_text() == "theirs"


def test_root_target_that_is_a_mount_point_is_used_as_given(
    tmp_path, mount_named_mounted
):
    src = tmp_path / "src"
    _write(src / "new.txt", "new")
    dst = tmp_path / "mounted"
    _write(dst / "theirs.txt", "theirs")

    PathSyncer(_size).sync(pathlib_next.LocalPath(src), pathlib_next.LocalPath(dst))

    assert (dst / "new.txt").read_text() == "new"
    assert (dst / "theirs.txt").read_text() == "theirs"


# --- the temporary sibling: replace fallback, name length, leftovers -------

import re
import time

from pathlib_next.mempath import MemPathBackend
from pathlib_next.utils.sync import _is_leftover, _temp_sibling

DAY = 24 * 60 * 60


class _RenamingMemPath(MemPath):
    """A MemPath with `rename()`, replacing an existing target."""

    def rename(self, target):
        target = target if isinstance(target, MemPath) else self.with_segments(target)
        parent, name = self._parent_container()
        target_parent, target_name = target._parent_container()
        target_parent[target_name] = parent.pop(name)
        return target


class _RefusingMemPath(_RenamingMemPath):
    """Refuses to rename onto an existing target, as SFTP without POSIX
    rename does."""

    def rename(self, target):
        target = target if isinstance(target, MemPath) else self.with_segments(target)
        if target.exists():
            raise FileExistsError(errno.EEXIST, "exists", str(target))
        return super().rename(target)


class _DroppingMemPath(_RefusingMemPath):
    """Refuses an existing target, and then the connection drops during the
    rename of a temporary file."""

    def rename(self, target):
        if _is_leftover(self.name) and not self.with_segments(target).exists():
            raise ConnectionResetError(errno.ECONNRESET, "connection reset")
        return super().rename(target)


def _mem_pair(target_cls, old="OLD VERSION", new="NEW VERSION!"):
    backend = MemPathBackend()
    target = target_cls("/dst", backend=backend)
    target.mkdir()
    (target / "f.txt").write_text(old)
    source = MemPath("/src")
    source.mkdir()
    (source / "f.txt").write_text(new)
    return source, target, backend


def _leftovers(directory):
    return sorted(name for name in directory if _is_leftover(name))


def test_rename_that_refuses_an_existing_target_still_replaces_it():
    source, target, backend = _mem_pair(_RefusingMemPath)

    PathSyncer(_size).sync(source, target)

    assert (target / "f.txt").read_text() == "NEW VERSION!"
    assert sorted(backend["dst"]) == ["f.txt"]


def test_failed_second_rename_keeps_the_new_version_under_its_temp_name():
    source, target, backend = _mem_pair(_DroppingMemPath)

    with pytest.raises(ConnectionResetError) as caught:
        PathSyncer(_size).sync(source, target)

    kept = _leftovers(backend["dst"])
    assert len(kept) == 1
    assert bytes(backend["dst"][kept[0]]) == b"NEW VERSION!"
    assert "f.txt" not in backend["dst"]
    assert kept[0] in str(caught.value)


def test_failed_second_rename_is_offered_to_the_policy_once_and_siblings_go_on():
    source, target, backend = _mem_pair(_DroppingMemPath)
    (source / "z.txt").write_text("later sibling")
    calls, ignore = _collect()

    PathSyncer(_size, ignore_error=ignore).sync(source, target)

    assert [(type(c[0]), c[3]) for c in calls] == [
        (ConnectionResetError, SyncEvent.Copy)
    ]
    assert len(_leftovers(backend["dst"])) == 1
    assert (target / "z.txt").read_text() == "later sibling"


def test_unlink_refused_during_the_fallback_keeps_the_old_version():
    class Stubborn(_RefusingMemPath):
        def unlink(self, missing_ok=False):
            if self.name == "f.txt":
                raise PermissionError(errno.EACCES, "denied", str(self))
            return super().unlink(missing_ok)

    source, target, backend = _mem_pair(Stubborn)

    with pytest.raises(PermissionError):
        PathSyncer(_size).sync(source, target)

    assert bytes(backend["dst"]["f.txt"]) == b"OLD VERSION"
    assert sorted(backend["dst"]) == ["f.txt"]


class _FailingRenameLocal(pathlib_next.LocalPath):
    """A local path whose rename of a temporary file fails."""

    def rename(self, target):
        if _is_leftover(self.name):
            raise ConnectionResetError(errno.ECONNRESET, "connection reset")
        return super().rename(target)


def test_failed_rename_of_a_replacing_link_keeps_the_temporary_link(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    _symlink("real.txt", source / "lnk")
    target = tmp_path / "dst"
    _write(target / "lnk", "previous regular file")

    with pytest.raises(ConnectionResetError) as caught:
        PathSyncer(_size, follow_symlinks=False).sync(
            pathlib_next.LocalPath(source), _FailingRenameLocal(target)
        )

    kept = _leftovers(os.listdir(target))
    assert len(kept) == 1
    assert os.readlink(target / kept[0]) == "real.txt"
    assert not os.path.lexists(target / "lnk")
    assert kept[0] in str(caught.value)


def test_failed_creation_of_a_replacing_link_keeps_the_previous_entry(tmp_path):
    class NoLinks(pathlib_next.LocalPath):
        def _symlink_to(self, target, target_is_directory=False):
            raise PermissionError(errno.EPERM, "denied", str(self))

    source = tmp_path / "src"
    source.mkdir()
    _symlink("real.txt", source / "lnk")
    target = tmp_path / "dst"
    _write(target / "lnk", "previous regular file")

    with pytest.raises(PermissionError):
        PathSyncer(_size, follow_symlinks=False).sync(
            pathlib_next.LocalPath(source), NoLinks(target)
        )

    assert (target / "lnk").read_text() == "previous regular file"
    assert os.listdir(target) == ["lnk"]


# the temporary name fits where the name does


def _limited(max_bytes=255):
    """A renaming MemPath that refuses to create a name longer than a
    filesystem accepts, recording every name it was asked to create."""

    class Limited(_RenamingMemPath):
        created = []

        def _open(self, mode="r", buffering=-1):
            if mode != "r":
                type(self).created.append(self.name)
                if len(self.name.encode("utf-8")) > max_bytes:
                    raise OSError(errno.ENAMETOOLONG, "name too long", str(self))
            return super()._open(mode, buffering)

    return Limited


@pytest.mark.parametrize(
    "name",
    [
        "n" * 246 + ".txt",
        "n" * 251 + ".txt",
        "é" * 125 + ".txt",
        "€" * 83 + ".txt",
    ],
    ids=["250-ascii", "255-ascii", "254-bytes-2byte", "253-bytes-3byte"],
)
def test_changed_file_with_a_long_name_is_updated_through_a_sibling(name):
    limited = _limited()
    source = MemPath("/src")
    source.mkdir()
    (source / name).write_text("first")
    target = limited("/dst")

    PathSyncer(_size).sync(source, target)
    (source / name).write_text("second, longer")
    limited.created.clear()
    PathSyncer(_size).sync(source, target)

    assert (target / name).read_text() == "second, longer"
    assert sorted(target.backend["dst"]) == [name]
    temps = [n for n in limited.created if n != name]
    assert len(temps) == 1 and _is_leftover(temps[0])
    assert len(temps[0].encode("utf-8")) <= 255


@pytest.mark.parametrize(
    "name", ["a", "n" * 255, "é" * 127, "€" * 85, "n" * 300, ".hidden"]
)
def test_temp_sibling_fits_the_name_limit_and_keeps_its_suffix(name):
    path = MemPath("/d") / name

    first, second = _temp_sibling(path), _temp_sibling(path)

    for temp in (first, second):
        assert temp.parent == path.parent
        assert len(temp.name.encode("utf-8")) <= 255
        assert _is_leftover(temp.name)
        assert temp.name.endswith(".pathlib-next-tmp")
    assert first != second
    if len(name.encode("utf-8")) <= 200:
        assert first.name.startswith("." + name + ".")


def test_changed_local_file_with_a_250_character_name_is_updated(tmp_path):
    prefix = "\\\\?\\" if IS_WINDOWS else ""
    name = "n" * 246 + ".txt"
    source = pathlib_next.LocalPath(prefix + str(tmp_path / "src"))
    target = pathlib_next.LocalPath(prefix + str(tmp_path / "dst"))
    source.mkdir()
    (source / name).write_text("first")
    try:
        PathSyncer(_size).sync(source, target)
    except OSError as error:
        pytest.skip(f"the filesystem does not accept the name: {error}")
    (source / name).write_text("second, longer")

    PathSyncer(_size).sync(source, target)

    assert (target / name).read_text() == "second, longer"
    assert [p.name for p in target.iterdir()] == [name]


# leftovers of a killed run


def _put(directory, name, text="partial", age=0.0):
    """A file in a MemPath directory, last modified `age` seconds ago (0 for
    an unknown time)."""
    path = directory / name
    path.write_text(text)
    parent, key = path._parent_container()
    parent[key].mtime = time.time() - age if age else 0.0
    return path


STALE = ".big.bin.aaaaaaaaaaaa.pathlib-next-tmp"
FRESH = ".big.bin.bbbbbbbbbbbb.pathlib-next-tmp"


def test_only_the_full_temp_pattern_is_a_leftover():
    assert _is_leftover(STALE)
    assert _is_leftover("..dot.0123456789ab.pathlib-next-tmp")
    for name in (
        "x-tmp",
        "x.pathlib-next-tmp",
        ".x.pathlib-next-tmp",
        ".x.0123456789AB.pathlib-next-tmp",
        ".x.0123456789a.pathlib-next-tmp",
        "..0123456789ab.pathlib-next-tmp",
        ".x.0123456789ab.pathlib-next-tmp.bak",
        "a.0123456789ab.pathlib-next-tmp",
    ):
        assert not _is_leftover(name), name


@pytest.mark.parametrize("remove_missing", [False, True])
def test_a_leftover_is_never_a_sync_source(remove_missing):
    source = MemPath("/src")
    source.mkdir()
    for name in ("big.bin", STALE, "report-tmp", ".x.pathlib-next-tmp"):
        (source / name).write_text("content")
    target = MemPath("/dst")
    events, hook = _events()

    PathSyncer(_size, remove_missing=remove_missing, hook=hook).sync(source, target)

    assert sorted(target.backend["dst"]) == [
        ".x.pathlib-next-tmp",
        "big.bin",
        "report-tmp",
    ]
    assert not any(STALE in s or STALE in t for _, s, t, _ in events)


def test_a_leftover_in_the_source_is_not_a_reason_to_remove_anything_else():
    source = MemPath("/src")
    source.mkdir()
    (source / "big.bin").write_text("content")
    (source / STALE).write_text("a leftover in the source")
    target = MemPath("/dst")
    target.mkdir()
    (target / "big.bin").write_text("content")
    (target / "keep.txt").write_text("target only")

    PathSyncer(_size, remove_missing=True).sync(source, target)

    assert sorted(target.backend["dst"]) == ["big.bin"]


@pytest.fixture
def leftover_layout():
    backend = MemPathBackend()
    target = _RenamingMemPath("/dst", backend=backend)
    target.mkdir()
    _put(target, "big.bin", "old")
    _put(target, STALE, age=2 * DAY)
    _put(target, FRESH, age=3600)
    _put(target, "x-tmp", age=2 * DAY)
    _put(target, ".big.bin.nothex.pathlib-next-tmp", age=2 * DAY)
    (target / ".dir.cccccccccccc.pathlib-next-tmp").mkdir()
    source = MemPath("/src")
    source.mkdir()
    (source / "big.bin").write_text("new content, longer")
    return source, target, backend


def test_stale_leftover_is_removed_where_a_file_is_rewritten(leftover_layout):
    source, target, backend = leftover_layout
    events, hook = _events()

    PathSyncer(_size, hook=hook).sync(source, target)

    assert bytes(backend["dst"]["big.bin"]) == b"new content, longer"
    assert STALE not in backend["dst"]
    # Younger, not the full pattern, or a directory: kept.
    assert sorted(backend["dst"]) == sorted(
        [
            "big.bin",
            FRESH,
            "x-tmp",
            ".big.bin.nothex.pathlib-next-tmp",
            ".dir.cccccccccccc.pathlib-next-tmp",
        ]
    )
    removed = [t for e, _, t, _ in events if e is SyncEvent.RemovedMissing]
    assert removed == ["/dst/" + STALE]


def test_dry_run_reports_the_stale_leftover_it_would_remove(leftover_layout):
    source, target, backend = leftover_layout
    before = sorted(backend["dst"])
    dry_events, dry_hook = _events()
    real_events, real_hook = _events()

    PathSyncer(_size, hook=dry_hook).sync(source, target, dry_run=True)

    assert sorted(backend["dst"]) == before
    PathSyncer(_size, hook=real_hook).sync(source, target)
    assert STALE not in backend["dst"]
    assert _mutations(dry_events) == _mutations(real_events)
    assert (SyncEvent.RemovedMissing, "/src/" + STALE, "/dst/" + STALE) in _mutations(
        dry_events
    )


def test_remove_missing_removes_a_stale_leftover_and_keeps_a_fresh_one(
    leftover_layout,
):
    source, target, backend = leftover_layout

    PathSyncer(_size, remove_missing=True).sync(source, target)

    assert sorted(backend["dst"]) == sorted(
        ["big.bin", FRESH, ".dir.cccccccccccc.pathlib-next-tmp"]
    )


def test_leftover_whose_age_is_unknown_is_kept():
    source, target, backend = _mem_pair(_RenamingMemPath)
    _put(target, STALE, age=0)

    PathSyncer(_size, remove_missing=True).sync(source, target)

    assert STALE in backend["dst"]
    assert (target / "f.txt").read_text() == "NEW VERSION!"


def test_temp_file_of_a_transfer_running_in_this_process_is_kept():
    # While a.bin is being written, a second sync into the same directory
    # looks at its temporary file, whose modification time reads as ancient.
    class Watched(_RenamingMemPath):
        during = None

        def _open(self, mode="r", buffering=-1):
            handle = super()._open(mode, buffering)
            callback, type(self).during = type(self).during, None
            if mode == "w" and _is_leftover(self.name) and callback:
                callback(self)
            return handle

    backend = MemPathBackend()
    target = Watched("/dst", backend=backend)
    target.mkdir()
    for name in ("a.bin", "c.bin"):
        _put(target, name, "old")
    first, second = MemPath("/one"), MemPath("/two")
    for tree, name in ((first, "a.bin"), (second, "c.bin")):
        tree.mkdir()
        (tree / name).write_text("new content")
    survived = []

    def meanwhile(temp):
        parent, key = temp._parent_container()
        parent[key].mtime = time.time() - 30 * DAY
        PathSyncer(_size).sync(second, target)
        survived.append(temp.exists())

    Watched.during = meanwhile
    PathSyncer(_size).sync(first, target)

    assert survived == [True]
    assert bytes(backend["dst"]["a.bin"]) == b"new content"
    assert bytes(backend["dst"]["c.bin"]) == b"new content"
    assert _leftovers(backend["dst"]) == []


# --- a dry run refuses what the real run refuses ------------------------------


def _link_source(tmp_path):
    source = tmp_path / "src"
    _write(source / "a.txt", "aaa")
    _symlink("a.txt", source / "lnk")
    _write(source / "z.txt", "zzz")
    return pathlib_next.LocalPath(source)


@pytest.mark.parametrize("remove_missing", [False, True])
def test_dry_run_raises_what_the_real_run_raises_for_a_target_without_links(
    tmp_path, remove_missing
):
    source = _link_source(tmp_path)
    outcomes = {}
    for dry_run in (True, False):
        target = MemPath("/dst")
        target.mkdir()
        (target / "lnk").write_text("existing entry")
        events, hook = _events()
        with pytest.raises(NotImplementedError) as caught:
            PathSyncer(
                _size, follow_symlinks=False, remove_missing=remove_missing, hook=hook
            ).sync(source, target, dry_run=dry_run)
        assert (target / "lnk").read_text() == "existing entry"
        outcomes[dry_run] = (str(caught.value), _mutations(events))

    assert outcomes[True] == outcomes[False]
    message, mutations = outcomes[True]
    assert "MemPath" in message
    assert all(event is not SyncEvent.Symlink for event, _, _ in mutations)


def test_dry_run_offers_the_same_refusal_to_the_policy_and_goes_on(tmp_path):
    source = _link_source(tmp_path)
    outcomes = {}
    for dry_run in (True, False):
        target = MemPath("/dst")
        target.mkdir()
        calls, ignore = _collect()
        events, hook = _events()

        PathSyncer(_size, follow_symlinks=False, hook=hook, ignore_error=ignore).sync(
            source, target, dry_run=dry_run
        )

        assert len(calls) == 1
        error, source_entry, target_entry, event = calls[0]
        assert isinstance(error, NotImplementedError)
        assert event is SyncEvent.Symlink
        assert source_entry.path.name == "lnk" and source_entry.is_symlink()
        assert target_entry.path == target / "lnk"
        assert (target / "z.txt").exists() is not dry_run
        outcomes[dry_run] = [
            (e, s, t) for e, s, t, _ in events if e is not SyncEvent.Synced
        ]

    assert outcomes[True] == outcomes[False]
    assert (SyncEvent.Error, str(source / "lnk"), "/dst/lnk") in outcomes[True]


# --- overlap is decided by where a path is, not how it is spelled ------------


def _mem_tree():
    backend = MemPathBackend()
    root = MemPath("/r", backend=backend)
    (root / "in").mkdir(parents=True)
    (root / "keep.txt").write_text("keep")
    # `/x/../r` is another spelling of `/r` only while `/x` is a directory.
    MemPath("/x", backend=backend).mkdir()
    return backend, root


@pytest.mark.parametrize(
    "spelling, overlap",
    [
        ("r", True),
        ("/r/", True),
        ("/x/../r", True),
        ("/r/./in", True),
        ("r/in", True),
        ("rr", False),
        ("/", True),
        ("/other", False),
    ],
)
def test_overlap_sees_one_mempath_tree_under_another_spelling(spelling, overlap):
    from pathlib_next.utils.sync import _paths_overlap

    backend, root = _mem_tree()
    other = MemPath(spelling, backend=backend)

    assert _paths_overlap(root, other) is overlap
    assert _paths_overlap(other, root) is overlap
    if overlap:
        for source, target in ((root, other), (other, root)):
            with pytest.raises(ValueError):
                PathSyncer(_size, remove_missing=True).sync(source, target)
            with pytest.raises(ValueError):
                PathSyncer(_size, remove_missing=True).sync(
                    source, target, dry_run=True
                )
        assert (root / "keep.txt").read_text() == "keep"
        assert sorted(backend["r"]) == ["in", "keep.txt"]
