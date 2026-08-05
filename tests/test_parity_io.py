"""LocalPath vs pathlib.Path on a real tmp_path tree. LocalPath *is*
pathlib.WindowsPath/PosixPath with our Path mixed in via MRO, so most of
these just confirm we haven't broken pathlib's own behavior, plus parity
for the handful of methods LocalPath explicitly overrides (touch, mkdir,
glob, rm/copy/move which have no direct pathlib.Path equivalent to diverge
from pre-3.14).
"""

import os

import pytest

import pathlib_next


def test_path_dispatcher_construction_from_str(tmp_path):
    # Regression: Path("...") (the abstract dispatcher, not LocalPath
    # directly) used to drop its constructor args entirely on Python
    # <3.12, where pathlib.Path.__new__ (not __init__) does the actual
    # _drv/_root/_parts parsing -- leaving a blank instance that crashed
    # the moment anything (e.g. `/`) touched that missing state. Masked on
    # 3.12+, where PurePath.__init__ does the parsing instead.
    p = pathlib_next.Path(str(tmp_path))
    assert isinstance(p, pathlib_next.LocalPath)
    child = p / "sub" / "f.txt"
    child.parent.mkdir(parents=True, exist_ok=True)
    child.write_text("data")
    assert child.read_text() == "data"


def test_touch_exist_ok_false_raises(tmp_path):
    p = pathlib_next.LocalPath(tmp_path) / "f.txt"
    p.touch()
    with pytest.raises(FileExistsError):
        p.touch(exist_ok=False)
    # B17 regression: must not have truncated the existing content.
    p.write_text("keep me")
    with pytest.raises(FileExistsError):
        p.touch(exist_ok=False)
    assert p.read_text() == "keep me"


def test_touch_creates_new_file(tmp_path):
    p = pathlib_next.LocalPath(tmp_path) / "new.txt"
    assert not p.exists()
    p.touch(exist_ok=False)
    assert p.exists()
    assert p.read_text() == ""


def test_mkdir_parents_exist_ok(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "a").mkdir()
    # B16 regression: parents=True must not choke on an already-existing
    # parent, and must still honor exist_ok=False for the leaf.
    (root / "a" / "b" / "c").mkdir(parents=True, exist_ok=True)
    assert (root / "a" / "b" / "c").is_dir()
    with pytest.raises(FileExistsError):
        (root / "a" / "b" / "c").mkdir(parents=True, exist_ok=False)


def test_glob_matches_pathlib(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    stdlib_root = fixture_tree
    ours = {p.name for p in root.glob("*.py")}
    theirs = {p.name for p in stdlib_root.glob("*.py")}
    assert ours == theirs == {"b.py"}


def test_glob_recursive_auto_enable(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    # Parity gap: "**" in the pattern auto-enables
    # recursion without passing recursive=True explicitly.
    ours = {p.name for p in root.glob("**/*.py")}
    theirs = {p.name for p in fixture_tree.glob("**/*.py")}
    assert ours == theirs == {"b.py", "c.py", "d.py"}


def test_glob_hidden_excluded_by_default(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    names = {p.name for p in root.glob("*.txt")}
    assert names == {"a.txt"}  # .hidden.txt excluded


def test_walk_matches_os_walk(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    ours = sorted(
        (
            str(p.relative_to(root).as_posix() if p != root else "."),
            sorted(d),
            sorted(f),
        )
        for p, d, f in root.walk()
    )
    theirs = sorted(
        (
            os.path.relpath(dirpath, fixture_tree).replace(os.sep, "/"),
            sorted(dirnames),
            sorted(filenames),
        )
        for dirpath, dirnames, filenames in os.walk(fixture_tree)
    )
    assert ours == theirs


def test_walk_top_down_false(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    ours_order = [p.name if p != root else "." for p, _, _ in root.walk(top_down=False)]
    theirs_order = [
        os.path.basename(dirpath) or "."
        for dirpath, _, _ in os.walk(fixture_tree, topdown=False)
    ]
    assert len(ours_order) == len(theirs_order)
    # bottom-up: deepest directories must come before their parents.
    assert ours_order.index("nested") < ours_order.index("sub")


def test_rm_recursive(fixture_tree):
    root = pathlib_next.LocalPath(fixture_tree)
    (root / "sub").rm(recursive=True)
    assert not (fixture_tree / "sub").exists()
    assert (fixture_tree / "a.txt").exists()


def test_rm_recursive_unlinks_symlink_to_directory(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    target = root / "target"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = root / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory symlink unavailable: {error}")

    link.rm(recursive=True)

    assert not os.path.lexists(os.fspath(link))
    assert target.is_dir()
    assert (target / "keep.txt").read_text() == "keep"


def test_rm_missing_ok(tmp_path):
    p = pathlib_next.LocalPath(tmp_path) / "missing"
    with pytest.raises(FileNotFoundError):
        p.rm()
    p.rm(missing_ok=True)


def test_rm_ignore_error_callable_invoked(tmp_path):
    # B14 regression: ignore_error callable must actually be called.
    calls = []
    p = pathlib_next.LocalPath(tmp_path) / "missing"
    p.rm(ignore_error=lambda err, path: calls.append((type(err), path)) or True)
    assert len(calls) == 1
    assert calls[0][0] is FileNotFoundError


def test_copy_into_existing_dir_raises(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "d").mkdir()
    (root / "f.txt").write_text("x")
    with pytest.raises(IsADirectoryError):
        (root / "f.txt").copy(root / "d")


def test_copy_overwrite(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "src.txt").write_text("src")
    (root / "dst.txt").write_text("dst")
    with pytest.raises(FileExistsError):
        (root / "src.txt").copy(root / "dst.txt")
    (root / "src.txt").copy(root / "dst.txt", overwrite=True)
    assert (root / "dst.txt").read_text() == "src"


def test_move_rename_fallback(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "src.txt").write_text("data")
    (root / "src.txt").move(root / "dst.txt")
    assert not (root / "src.txt").exists()
    assert (root / "dst.txt").read_text() == "data"


def test_copy_recursive(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    src = root / "src"
    src.mkdir()
    (src / "f1.txt").write_text("1")
    (src / "sub").mkdir()
    (src / "sub" / "f2.txt").write_text("2")

    dst = root / "dst"
    src.copy(dst, recursive=True)

    assert (dst / "f1.txt").read_text() == "1"
    assert (dst / "sub" / "f2.txt").read_text() == "2"

    # Test FileExistsError when not overwrite
    with pytest.raises(FileExistsError):
        src.copy(dst, recursive=True)

    # Test overwrite
    (src / "f1.txt").write_text("1-updated")
    src.copy(dst, recursive=True, overwrite=True)
    assert (dst / "f1.txt").read_text() == "1-updated"


def test_local_copy_and_move_resolve_to_pathlib_next():
    assert pathlib_next.LocalPath.copy.__module__.startswith("pathlib_next.")
    assert pathlib_next.LocalPath.move.__module__.startswith("pathlib_next.")


def test_copy_progress_hook_monotonic_and_reaches_total(tmp_path):
    # Copy progress hook (BinaryOpen.copy level): bytes_copied must
    # increase monotonically and the final call must report the file's
    # full size.
    root = pathlib_next.LocalPath(tmp_path)
    data = b"x" * (256 * 1024 + 17)  # not a multiple of chunk_size
    (root / "src.bin").write_bytes(data)

    # Path.copy() doesn't take chunk_size directly; exercise the lower
    # level BinaryOpen.copy() for chunk_size control, and Path.copy()
    # separately below for the public surface.
    from pathlib_next.protocols.io import BinaryOpen

    calls = []

    BinaryOpen.copy(
        root / "src.bin",
        root / "dst2.bin",
        chunk_size=64 * 1024,
        progress=lambda copied, total: calls.append((copied, total)),
    )

    assert (root / "dst2.bin").read_bytes() == data
    assert len(calls) >= 2
    # Monotonically increasing byte counts.
    counts = [c for c, _ in calls]
    assert counts == sorted(counts)
    assert all(b > a for a, b in zip(counts, counts[1:]))
    # Final call reaches the file's total size, and total_size is stable
    # and correct throughout (known from stat()).
    assert calls[-1][0] == len(data)
    assert all(total == len(data) for _, total in calls)


def test_copy_progress_hook_not_called_without_callback_and_no_behavior_change(
    tmp_path,
):
    # No callback => identical behavior/content to plain shutil.copyfileobj
    # (Phase 1's "no behavior change" requirement).
    root = pathlib_next.LocalPath(tmp_path)
    data = b"hello world" * 1000
    (root / "src.bin").write_bytes(data)
    (root / "src.bin").copy(root / "dst.bin")
    assert (root / "dst.bin").read_bytes() == data


def test_copy_progress_hook_fires_on_path_copy(tmp_path):
    # Public Path.copy() surface: progress(path, bytes_copied, total_size),
    # path identifies the file being streamed.
    root = pathlib_next.LocalPath(tmp_path)
    data = b"y" * 1000
    src = root / "src.bin"
    src.write_bytes(data)
    dst = root / "dst.bin"

    calls = []
    src.copy(
        dst, progress=lambda path, copied, total: calls.append((path, copied, total))
    )

    assert calls
    assert all(path == src for path, _, _ in calls)
    assert calls[-1][1] == len(data)
    assert calls[-1][2] == len(data)


def test_copy_recursive_progress_hook_reports_per_file_identity(tmp_path):
    # Phase 2: recursive copy must report per-file identity alongside byte
    # progress -- not just an anonymous byte stream.
    root = pathlib_next.LocalPath(tmp_path)
    src = root / "src"
    src.mkdir()
    (src / "f1.txt").write_bytes(b"a" * 500)
    (src / "sub").mkdir()
    (src / "sub" / "f2.txt").write_bytes(b"b" * 700)

    dst = root / "dst"

    calls = []
    src.copy(
        dst,
        recursive=True,
        progress=lambda path, copied, total: calls.append((path, copied, total)),
    )

    assert (dst / "f1.txt").read_bytes() == b"a" * 500
    assert (dst / "sub" / "f2.txt").read_bytes() == b"b" * 700

    seen_paths = {path for path, _, _ in calls}
    assert seen_paths == {src / "f1.txt", src / "sub" / "f2.txt"}

    # Per file: monotonic and final call equals that file's total size.
    by_path = {}
    for path, copied, total in calls:
        by_path.setdefault(path, []).append((copied, total))

    f1_calls = by_path[src / "f1.txt"]
    f1_counts = [c for c, _ in f1_calls]
    assert f1_counts == sorted(f1_counts)
    assert f1_calls[-1] == (500, 500)

    f2_calls = by_path[src / "sub" / "f2.txt"]
    f2_counts = [c for c, _ in f2_calls]
    assert f2_counts == sorted(f2_counts)
    assert f2_calls[-1] == (700, 700)


def test_move_recursive_fallback(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    src = root / "src"
    src.mkdir()
    (src / "f1.txt").write_text("1")
    (src / "sub").mkdir()
    (src / "sub" / "f2.txt").write_text("2")

    class NoRenamePath(pathlib_next.LocalPath):
        def rename(self, target):
            raise NotImplementedError("rename not supported")

    src_no_rename = NoRenamePath(src)
    dst = root / "dst_moved"

    src_no_rename.move(dst)
    assert not src.exists()
    assert (dst / "f1.txt").read_text() == "1"
    assert (dst / "sub" / "f2.txt").read_text() == "2"


# --- symlink_to(force=) ---------------------------------------------------
#
# `force=` is a pathlib_next extension (docs/divergences.md): stdlib's
# symlink_to() raises FileExistsError when something is already at the link
# path, and every consumer that wanted "replace it" re-implemented the same
# unlink-then-symlink dance. Because no stdlib version accepts the keyword,
# LocalPath only honors it via the `_OPERATION_NAMES` guard -- so these run
# against LocalPath deliberately: they are the regression that the guard,
# and the generic `symlink_to`/`_symlink_to` split, actually took effect on
# the class where stdlib would otherwise win.


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")


def test_symlink_to_without_force_matches_stdlib(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "target.txt").write_text("target")
    link = root / "link"
    _symlink_or_skip(link, root / "target.txt")

    assert link.is_symlink()
    assert link.read_text() == "target"

    # Default is stdlib-exact: an existing entry is never removed.
    with pytest.raises(FileExistsError):
        link.symlink_to(root / "target.txt")


def test_symlink_to_force_replaces_existing_symlink(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    link = root / "link"
    _symlink_or_skip(link, root / "a.txt")
    assert link.read_text() == "a"

    link.symlink_to(root / "b.txt", force=True)
    assert link.is_symlink()
    assert link.read_text() == "b"


def test_symlink_to_force_replaces_existing_regular_file(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "target.txt").write_text("target")
    link = root / "link"
    link.write_text("i am a real file")
    assert not link.is_symlink()

    link.symlink_to(root / "target.txt", force=True)
    assert link.is_symlink()
    assert link.read_text() == "target"


def test_symlink_to_force_on_missing_path_is_plain_create(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "target.txt").write_text("target")
    link = root / "link"

    # force= must not require the path to exist -- unlink(missing_ok=True).
    try:
        link.symlink_to(root / "target.txt", force=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")
    assert link.is_symlink()
    assert link.read_text() == "target"


def test_symlink_to_force_refuses_to_remove_a_directory(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "target.txt").write_text("target")
    link = root / "link"
    link.mkdir()
    (link / "keep.txt").write_text("keep")

    # force= replaces an entry, it does not delete a tree. The directory
    # and its contents must survive, and the call must still fail.
    with pytest.raises(OSError):
        link.symlink_to(root / "target.txt", force=True)
    assert link.is_dir()
    assert (link / "keep.txt").read_text() == "keep"


def test_symlink_to_accepts_str_target(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "target.txt").write_text("target")
    link = root / "link"

    # A str target is normalized to a path object before reaching the
    # backend primitive (same `type(self)(target)` form copy()/move() use).
    _symlink_or_skip(link, str(root / "target.txt"))
    assert link.read_text() == "target"


def test_symlink_to_relative_target_stays_relative(tmp_path):
    root = pathlib_next.LocalPath(tmp_path)
    (root / "target.txt").write_text("target")
    link = root / "link"

    # readlink() reports the stored target as-is, so normalization must not
    # silently absolutize a relative target.
    _symlink_or_skip(link, "target.txt")
    assert link.readlink().as_posix() == "target.txt"
