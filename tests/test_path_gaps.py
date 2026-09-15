import pytest
import unittest.mock
from pathlib_next.mempath import MemPath
from pathlib_next.utils.stat import FileStat


def test_rm_ignore_error_callable_false():
    root = MemPath("/")
    # File does not exist, so rm() raises FileNotFoundError.
    p = root / "nonexistent"

    # Callable returns False -> error should be raised.
    with pytest.raises(FileNotFoundError):
        p.rm(ignore_error=lambda err, path: False)


def test_rm_ignore_error_bool_false():
    root = MemPath("/")
    p = root / "nonexistent"

    # ignore_error=False -> error should be raised.
    with pytest.raises(FileNotFoundError):
        p.rm(ignore_error=False)


def test_rm_ignore_error_bool_true():
    root = MemPath("/")
    p = root / "nonexistent"

    # ignore_error=True -> error should be ignored.
    p.rm(ignore_error=True)


def test_move_rename_fallback_to_copy_unlink():
    root = MemPath("/")
    src = root / "src.txt"
    dst = root / "dst.txt"
    src.write_text("hello")

    # Since MemPath.rename is not implemented, this exercises the move fallback.
    src.move(dst)

    assert dst.read_text() == "hello"
    assert not src.exists()


def test_copy_chmod_not_implemented():
    root = MemPath("/")
    src = root / "src.txt"
    dst = root / "dst.txt"
    src.write_text("hello")

    # MemPath does not implement chmod, so this exercises copy catching NotImplementedError.
    src.copy(dst)
    assert dst.read_text() == "hello"


def test_samefile_not_implemented():
    root = MemPath("/")
    p1 = root / "f1.txt"
    p2 = root / "f2.txt"
    p1.write_text("x")
    p2.write_text("y")
    with pytest.raises(NotImplementedError) as exc:
        p1.samefile(p2)
    assert "requires stat() to provide st_dev/st_ino" in str(exc.value)


def test_walk_oserror_isdir():
    root = MemPath("/")
    (root / "sub").mkdir()

    # Mock FileStat.from_path to raise OSError
    original_from_path = FileStat.from_path

    def mocked_from_path(entry, **kwargs):
        if entry.name == "sub":
            raise OSError("Stat failed")
        return original_from_path(entry, **kwargs)

    with unittest.mock.patch.object(FileStat, "from_path", mocked_from_path):
        # The walk should run and treat "sub" as a non-directory (so filenames, not dirnames)
        results = list(root.walk())
        assert len(results) == 1
        path, dirnames, filenames = results[0]
        assert "sub" in filenames
        assert "sub" not in dirnames


def test_touch_exist_ok_true():
    root = MemPath("/")
    f = root / "f.txt"
    f.touch()
    assert f.exists()
    # Should return early and do nothing
    f.touch(exist_ok=True)


def test_touch_open_x_not_implemented():
    class NoXMemPath(MemPath):
        def _open(self, mode="r", buffering=-1):
            if mode == "x":
                raise NotImplementedError("x not supported")
            return super()._open(mode, buffering)

    root = NoXMemPath("/")
    f = root / "new_touch.txt"
    # touch(exist_ok=False) will try "x", catch NotImplementedError, and fallback
    f.touch(exist_ok=False)
    assert f.exists()

    # If the file already exists, it should raise FileExistsError in the fallback check
    with pytest.raises(FileExistsError):
        f.touch(exist_ok=False)


# --- ignore_error: bool-or-callable consistency across call sites ----------
# `Path.rm()`, `Path.copy()` and `PathSyncer.sync()` each accept a bool OR a
# callable. The callable ARITIES differ per call site by design (rm ->
# (error, path), copy -> (error), sync -> (error, source, target, event)), so
# only the bool case is normalized; these tests pin both halves.


class _FailingChildCopy(MemPath):
    """MemPath whose "boom.txt" child raises on read, to drive the error
    path of `copy(recursive=True)`'s per-child try/except."""

    def _open(self, mode="r", buffering=-1):
        if self.name == "boom.txt" and mode == "r":
            raise OSError("copy failed")
        return super()._open(mode, buffering)


def _copy_tree():
    root = _FailingChildCopy("/src")
    root.mkdir()
    (root / "ok.txt").write_text("fine")
    (root / "boom.txt").write_text("bad")
    return root


def test_copy_recursive_ignore_error_bool_true_suppresses():
    src = _copy_tree()
    dst = _FailingChildCopy("/dst", backend=src.backend)
    # bool True is newly accepted; previously this raised
    # `TypeError: 'bool' object is not callable`-adjacent breakage because
    # only a callable-or-None was handled.
    src.copy(dst, recursive=True, ignore_error=True)
    # The good sibling still copies -- the failing child was tolerated
    # rather than aborting the whole recursive copy.
    assert (dst / "ok.txt").read_text() == "fine"


def test_copy_recursive_ignore_error_bool_false_raises():
    src = _copy_tree()
    dst = _FailingChildCopy("/dst", backend=src.backend)
    with pytest.raises(OSError):
        src.copy(dst, recursive=True, ignore_error=False)


def test_copy_recursive_ignore_error_none_raises():
    # None keeps its documented meaning: fail on the first error. Adding
    # bool support must not change what None does.
    src = _copy_tree()
    dst = _FailingChildCopy("/dst", backend=src.backend)
    with pytest.raises(OSError):
        src.copy(dst, recursive=True, ignore_error=None)


def test_copy_recursive_ignore_error_callable_is_notified_and_suppresses():
    # Backward compatibility: copy()'s callable is a NOTIFICATION hook whose
    # return value is not consulted (callers such as `errors.append` return
    # None and still expect suppression).
    src = _copy_tree()
    dst = _FailingChildCopy("/dst", backend=src.backend)
    errors = []
    src.copy(dst, recursive=True, ignore_error=errors.append)
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)
    assert (dst / "ok.txt").read_text() == "fine"


def test_copy_recursive_ignore_error_callable_returning_false_still_suppresses():
    src = _copy_tree()
    dst = _FailingChildCopy("/dst", backend=src.backend)
    calls = []
    src.copy(dst, recursive=True, ignore_error=lambda e: calls.append(e) or False)
    assert len(calls) == 1


def test_rm_ignore_error_callable_true_suppresses():
    root = MemPath("/")
    (root / "nonexistent").rm(ignore_error=lambda err, path: True)


# --- mode/owner normalization (2026-08-04 findings) -----------------------
#
# Both helpers live in utils because the value they normalize is accepted at
# several entry points; centralizing them is what stops the semantics from
# drifting between backends.


def test_as_mode_parses_string_as_octal():
    from pathlib_next import utils

    # The whole point: "0755" is base 8, never base 10. int("0755") would be
    # 755 == 0o1363, a different *and valid* mode -- so a wrong answer here
    # sets plausible-but-unintended permissions and nothing raises.
    assert utils.as_mode("0755") == 0o755
    assert utils.as_mode("755") == 0o755
    assert utils.as_mode("0o755") == 0o755
    assert utils.as_mode(0o755) == 0o755
    assert utils.as_mode("0644") != 644


@pytest.mark.parametrize("bad", ["0899", "abc", "", "7 5", "-755"])
def test_as_mode_rejects_non_octal(bad):
    from pathlib_next import utils

    with pytest.raises(ValueError):
        utils.as_mode(bad)


def test_as_owner_canonicalizes_unchanged_sentinels():
    from pathlib_next import utils

    # -1 (os.chown's spelling) and None both mean "leave unchanged".
    assert utils.as_owner(None, None) == (None, None)
    assert utils.as_owner(-1, -1) == (None, None)
    assert utils.as_owner(-1, 1000) == (None, 1000)
    # uid 0 is root, not "unset" -- a falsy check would drop it.
    assert utils.as_owner(0, 0) == (0, 0)
    # Names pass through for backends that can resolve them.
    assert utils.as_owner("root", "wheel") == ("root", "wheel")


def test_chown_with_no_changes_does_not_reach_the_backend():
    called = []

    class _P(MemPath):
        __slots__ = ()

        def _chown(self, uid, gid, *, follow_symlinks=True):
            called.append((uid, gid))

    p = _P("/x")
    p.chown()
    p.chown(-1, -1)
    assert called == []
    p.chown(gid=1000)
    assert called == [(None, 1000)]


def test_chown_is_not_implemented_by_default():
    # A backend that hasn't implemented the primitive must say so rather
    # than silently no-op, matching how the rest of the library treats
    # unsupported operations.
    with pytest.raises(NotImplementedError):
        MemPath("/x").chown(1000, 1000)


# --- rm(recursive=True) error handling inside a tree --------------------------
# Every backend without its own rm() relies on these branches. Failures are
# injected per path name through a MemPath subclass.


class _FaultyMemPath(MemPath):
    """MemPath whose operations raise PermissionError for chosen names
    (class attributes, so children made via with_segments() share them)."""

    fail_unlink = frozenset()
    fail_rmdir = frozenset()
    fail_scandir = frozenset()
    fail_stat = frozenset()
    unseeded_listing = False

    def unlink(self, missing_ok=False):
        if self.name in self.fail_unlink:
            raise PermissionError(13, "unlink refused", str(self))
        return super().unlink(missing_ok=missing_ok)

    def rmdir(self):
        if self.name in self.fail_rmdir:
            raise PermissionError(13, "rmdir refused", str(self))
        return super().rmdir()

    def stat(self, *, follow_symlinks=True):
        if self.name in self.fail_stat:
            raise PermissionError(13, "stat refused", str(self))
        return super().stat(follow_symlinks=follow_symlinks)

    def _scandir(self):
        if self.name in self.fail_scandir:
            raise PermissionError(13, "listing refused", str(self))
        for name, stat in super()._scandir():
            yield name, (None if self.unseeded_listing else stat)


def _faulty_tree(**faults):
    cls = type("_Faulty", (_FaultyMemPath,), faults)
    root = cls("/")
    (root / "d").mkdir()
    (root / "d" / "locked.txt").write_text("x")
    (root / "d" / "other.txt").write_text("y")
    (root / "d" / "sub").mkdir()
    (root / "d" / "sub" / "z.txt").write_text("z")
    return root


def _calls_as_names(calls):
    return [(type(error).__name__, str(path)) for error, path in calls]


def test_rm_recursive_child_unlink_failure_reported_then_parent_rmdir():
    import errno

    root = _faulty_tree(fail_unlink=frozenset({"locked.txt"}))
    calls = []
    (root / "d").rm(
        recursive=True, ignore_error=lambda e, p: calls.append((e, p)) or True
    )
    # The child's failure, then the parent's own ENOTEMPTY (the parent really
    # was not removed): each failure once, with the path it happened on.
    assert _calls_as_names(calls) == [
        ("PermissionError", "/d/locked.txt"),
        ("OSError", "/d"),
    ]
    assert calls[1][0].errno == errno.ENOTEMPTY
    assert [p.name for p in (root / "d").iterdir()] == ["locked.txt"]


def test_rm_recursive_child_failure_propagates_without_ignore_error():
    root = _faulty_tree(fail_unlink=frozenset({"locked.txt"}))
    with pytest.raises(PermissionError, match="unlink refused"):
        (root / "d").rm(recursive=True)
    assert (root / "d" / "locked.txt").exists()


def test_rm_recursive_callback_returning_false_reraises():
    root = _faulty_tree(fail_rmdir=frozenset({"sub"}))
    seen = []
    with pytest.raises(PermissionError, match="rmdir refused"):
        (root / "d").rm(recursive=True, ignore_error=lambda e, p: seen.append(p))
    # Consulted once: the declined error is not re-offered to the handler by
    # each enclosing directory on its way out.
    assert [str(p) for p in seen] == ["/d/sub"]


def test_rm_recursive_listing_failure_is_reported_for_that_directory():
    root = _faulty_tree(fail_scandir=frozenset({"sub"}))
    calls = []
    (root / "d").rm(
        recursive=True, ignore_error=lambda e, p: calls.append((e, p)) or True
    )
    # The unlistable directory is reported and left alone (no rmdir attempt
    # of its own); its parent then fails ENOTEMPTY.
    assert _calls_as_names(calls) == [
        ("PermissionError", "/d/sub"),
        ("OSError", "/d"),
    ]
    assert (root / "d" / "sub" / "z.txt").read_text() == "z"
    assert not (root / "d" / "other.txt").exists()


def test_rm_recursive_stats_children_the_listing_did_not_seed():
    root = _faulty_tree(unseeded_listing=True)
    (root / "d").rm(recursive=True)
    assert not (root / "d").exists()


def test_rm_top_level_stat_failure_is_not_missing():
    root = _faulty_tree(fail_stat=frozenset({"d"}))
    with pytest.raises(PermissionError, match="stat refused"):
        (root / "d").rm(recursive=True, missing_ok=True)
    calls = []
    (root / "d").rm(
        recursive=True, ignore_error=lambda e, p: calls.append((e, p)) or True
    )
    assert _calls_as_names(calls) == [("PermissionError", "/d")]
    assert (root / "d" / "sub" / "z.txt").exists()


def test_rm_file_unlink_failure_goes_through_ignore_error():
    root = _faulty_tree(fail_unlink=frozenset({"locked.txt"}))
    with pytest.raises(PermissionError):
        (root / "d" / "locked.txt").rm()
    (root / "d" / "locked.txt").rm(ignore_error=True)
    assert (root / "d" / "locked.txt").exists()


def test_rm_recursive_accepts_os_direntry_listing(tmp_path):
    # The adapter for a `_scandir()` yielding os.DirEntry objects (as the
    # stdlib's own private `_scandir` does) instead of (name, stat) tuples.
    import os

    from pathlib_next import LocalPath

    class _DirEntryLocalPath(LocalPath):
        def _scandir(self):
            with os.scandir(self) as entries:
                yield from list(entries)

    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "a.txt").write_text("a")
    (tmp_path / "d" / "sub" / "b.txt").write_text("b")
    target = _DirEntryLocalPath(tmp_path / "d")
    assert not isinstance(next(iter(target._scandir())), tuple)
    target.rm(recursive=True)
    assert not (tmp_path / "d").exists()
