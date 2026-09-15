"""I/O parity with pathlib for the generic `Path` implementations: `touch()`
(no default chmod, no truncation, FileUri umask/mtime) and
`copy(preserve_metadata=True)` (only a backend-reported mode is applied).
"""

import os
import stat

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.uri import UriPath
from pathlib_next.utils.stat import FileStat
from pathlib_next.utils.sync import PathSyncer

# --- touch() on a generic backend -----------------------------------------


class _ChmodRecordingMemPath(MemPath):
    """MemPath with a chmod() that records its calls, standing in for any
    backend whose chmod is a verbatim remote setstat (SFTP, FTP SITE CHMOD)."""

    def chmod(self, mode, *, follow_symlinks=True):
        self.backend.setdefault("__chmod_calls__", []).append(mode)


def _chmod_calls(path):
    return path.backend.get("__chmod_calls__", [])


def test_generic_touch_does_not_chmod_a_new_file_by_default():
    root = _ChmodRecordingMemPath("/")
    f = root / "flag"
    f.touch()
    assert f.exists()
    # pathlib's touch never calls chmod; the old default chmod'ed 0o666.
    assert _chmod_calls(f) == []


def test_generic_touch_exist_ok_false_does_not_chmod_by_default():
    root = _ChmodRecordingMemPath("/")
    (root / "flag").touch(exist_ok=False)
    assert _chmod_calls(root) == []


def test_generic_touch_applies_an_explicit_mode_to_a_new_file():
    root = _ChmodRecordingMemPath("/")
    (root / "flag").touch(mode=0o600)
    assert _chmod_calls(root) == [0o600]


def test_generic_touch_does_not_chmod_an_existing_file():
    root = _ChmodRecordingMemPath("/")
    f = root / "flag"
    f.write_bytes(b"data")
    f.touch(mode=0o600)
    assert _chmod_calls(root) == []
    assert f.read_bytes() == b"data"


class _FlakyStatMemPath(MemPath):
    """stat() fails once with the given error, then behaves normally."""

    def stat(self, *, follow_symlinks=True):
        pending = self.backend.pop("__stat_error__", None)
        if pending is not None:
            raise pending
        return super().stat(follow_symlinks=follow_symlinks)


def test_generic_touch_transient_stat_error_propagates_without_truncating():
    root = _FlakyStatMemPath("/")
    f = root / "data.bin"
    f.write_bytes(b"x" * 1000)
    root.backend["__stat_error__"] = TimeoutError("stat timed out")
    # exists() swallows any OSError, so the old touch() read this as
    # "missing" and truncated the file with open("w").
    with pytest.raises(TimeoutError):
        f.touch()
    assert f.read_bytes() == b"x" * 1000


def test_generic_touch_file_created_after_stat_is_not_truncated():
    root = _FlakyStatMemPath("/")
    f = root / "data.bin"
    f.write_bytes(b"keep")
    # stat() says missing, but the file exists by the time touch opens it
    # (a concurrent creator): open("x") raises FileExistsError.
    root.backend["__stat_error__"] = FileNotFoundError("gone... or not")
    f.touch()
    assert f.read_bytes() == b"keep"

    root.backend["__stat_error__"] = FileNotFoundError("gone... or not")
    with pytest.raises(FileExistsError):
        f.touch(exist_ok=False)
    assert f.read_bytes() == b"keep"


def test_generic_touch_existing_file_keeps_content():
    root = MemPath("/")
    f = root / "a.txt"
    f.write_text("content")
    f.touch()
    assert f.read_text() == "content"


# --- FileUri.touch delegates to pathlib ------------------------------------


def _file_uri(path):
    return UriPath(pathlib_next.LocalPath(path))


def test_fileuri_touch_does_not_chmod(tmp_path, monkeypatch):
    calls = []
    real_chmod = pathlib_next.LocalPath.chmod

    def spy(self, mode, *, follow_symlinks=True):
        calls.append(mode)
        return real_chmod(self, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(pathlib_next.LocalPath, "chmod", spy)
    uri = _file_uri(tmp_path / "new.txt")
    assert type(uri).__name__ == "FileUri"
    uri.touch()
    uri2 = _file_uri(tmp_path / "new2.txt")
    uri2.touch(exist_ok=False)
    assert (tmp_path / "new.txt").is_file()
    assert (tmp_path / "new2.txt").is_file()
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits and umask")
def test_fileuri_touch_mode_is_umask_masked_like_pathlib(tmp_path):
    old = os.umask(0o022)
    try:
        _file_uri(tmp_path / "default.txt").touch()
        _file_uri(tmp_path / "explicit.txt").touch(0o666)
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(tmp_path / "default.txt").st_mode) == 0o644
    assert stat.S_IMODE(os.stat(tmp_path / "explicit.txt").st_mode) == 0o644


def test_fileuri_touch_bumps_mtime_of_existing_file(tmp_path):
    f = tmp_path / "old.txt"
    f.write_text("keep")
    os.utime(f, (1000, 1000))
    _file_uri(f).touch()
    assert os.stat(f).st_mtime > 1000
    assert f.read_text() == "keep"


def test_fileuri_touch_exist_ok_false_raises(tmp_path):
    f = tmp_path / "exists.txt"
    f.write_text("keep")
    with pytest.raises(FileExistsError):
        _file_uri(f).touch(exist_ok=False)
    assert f.read_text() == "keep"


# --- FileStat.mode_known ---------------------------------------------------


def test_filestat_synthesized_mode_is_not_known():
    assert FileStat().mode_known is False
    assert FileStat(is_dir=True).mode_known is False
    # The placeholder is still there for is_file()/is_dir().
    assert FileStat().st_mode == stat.S_IFREG | 0o444
    assert FileStat(is_dir=True).st_mode == stat.S_IFDIR | 0o555


def test_filestat_given_mode_is_known():
    st = FileStat(st_mode=stat.S_IFREG | 0o640)
    assert st.mode_known is True
    assert st.st_mode == stat.S_IFREG | 0o640


def test_filestat_setmode_makes_mode_known():
    st = FileStat()
    st.setmode(0o600)
    assert st.mode_known is True
    assert st.st_mode == stat.S_IFREG | 0o600


def test_filestat_from_real_stat_is_known(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    assert FileStat.from_stat(os.stat(f)).mode_known is True


def test_filestat_from_stat_without_mode_is_not_known():
    class NoMode:
        st_size = 3

    assert FileStat.from_stat(NoMode()).mode_known is False


def test_filestat_items_reports_stat_fields_only():
    assert [k for k, _v in FileStat().items()] == [
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_atime",
        "st_mtime",
        "st_ctime",
    ]


# --- copy(preserve_metadata=True) ------------------------------------------


def _writable(path):
    return os.access(path, os.W_OK)


def test_copy_from_synthetic_mode_backend_leaves_target_writable(tmp_path):
    src = MemPath("/a.txt")
    src.write_text("one")
    target = pathlib_next.LocalPath(tmp_path / "a.txt")
    src.copy(target)
    assert target.read_text() == "one"
    # MemPath reports the placeholder 0o444, which used to be chmod'ed onto
    # the target, making it read-only.
    assert _writable(target)
    target.write_text("local edit")
    src.write_text("two")
    src.copy(target, overwrite=True)
    assert target.read_text() == "two"


def test_copy_skips_chmod_for_synthetic_mode():
    calls = []

    class Target(MemPath):
        def chmod(self, mode, *, follow_symlinks=True):
            calls.append(mode)

    src = MemPath("/a.txt")
    src.write_text("x")
    src.copy(Target("/b.txt"))
    assert calls == []


def test_copy_applies_a_backend_reported_mode():
    calls = []

    class Source(MemPath):
        def stat(self, *, follow_symlinks=True):
            st = super().stat(follow_symlinks=follow_symlinks)
            st.setmode(0o640)
            return st

    class Target(MemPath):
        def chmod(self, mode, *, follow_symlinks=True):
            calls.append(mode)

    src = Source("/a.txt")
    src.write_text("x")
    src.copy(Target("/b.txt"))
    assert calls == [stat.S_IFREG | 0o640]
    src.copy(Target("/c.txt"), preserve_metadata=False)
    assert calls == [stat.S_IFREG | 0o640]


def test_copy_between_local_paths_still_preserves_a_real_readonly_mode(tmp_path):
    src = pathlib_next.LocalPath(tmp_path / "ro.txt")
    src.write_text("x")
    os.chmod(src, stat.S_IREAD)
    target = pathlib_next.LocalPath(tmp_path / "copy.txt")
    try:
        src.copy(target)
        assert not _writable(target)
    finally:
        os.chmod(src, stat.S_IREAD | stat.S_IWRITE)
        if os.path.exists(target):
            os.chmod(target, stat.S_IREAD | stat.S_IWRITE)


def test_sync_from_synthetic_mode_backend_can_resync(tmp_path):
    def checksum(entry):
        return entry.stat.st_size

    root = MemPath("/")
    (root / "a.txt").write_text("aaa")
    target = pathlib_next.LocalPath(tmp_path)
    PathSyncer(checksum).sync(root, target)
    assert _writable(tmp_path / "a.txt")
    (root / "a.txt").write_text("changed content")
    # The second sync replaces the file; a read-only first copy made this
    # raise PermissionError on Windows.
    PathSyncer(checksum).sync(root, target)
    assert (tmp_path / "a.txt").read_text() == "changed content"
