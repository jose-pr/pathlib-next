"""Generic copy/move/rm must never destroy data they were not asked to.

Each test asserts what has to SURVIVE (the destination, the source, files
outside the tree) -- "an exception was raised" alone passed against the
defects these guard: a failed copy emptied its target, a same-file copy or
case-only move deleted the file, move(overwrite=True) removed the target
before discovering the source was missing, and rm(recursive=True) walked
through Windows junctions and file: directory symlinks.
"""

import os
import pathlib
import subprocess
import sys

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.utils.sync import PathSyncer

LocalPath = pathlib_next.LocalPath


# --- copy -----------------------------------------------------------------


def test_copy_missing_source_leaves_existing_target_intact(tmp_path):
    target = LocalPath(tmp_path / "dst.txt")
    target.write_text("PRECIOUS")
    with pytest.raises(FileNotFoundError):
        LocalPath(tmp_path / "missing.txt").copy(target, overwrite=True)
    assert target.read_text() == "PRECIOUS"


def test_copy_missing_source_creates_no_empty_target(tmp_path):
    target = LocalPath(tmp_path / "new.txt")
    with pytest.raises(FileNotFoundError):
        LocalPath(tmp_path / "missing.txt").copy(target)
    assert not target.exists()


def test_copy_directory_without_recursive_leaves_target_intact(tmp_path):
    src = LocalPath(tmp_path / "adir")
    src.mkdir()
    target = LocalPath(tmp_path / "dst.txt")
    target.write_text("PRECIOUS")
    with pytest.raises(OSError):
        src.copy(target, overwrite=True)
    assert target.read_text() == "PRECIOUS"


def test_copy_http_404_source_leaves_local_target_intact(http_server, tmp_path):
    pytest.importorskip("requests")
    from pathlib_next.uri import UriPath

    target = LocalPath(tmp_path / "cache.json")
    target.write_text("GOOD CACHE")
    with pytest.raises(FileNotFoundError):
        UriPath(http_server + "/no-such-file.json").copy(target, overwrite=True)
    assert target.read_text() == "GOOD CACHE"


def test_copy_failing_mid_stream_removes_partial_target(monkeypatch):
    root = MemPath("/")
    src = root / "src.bin"
    src.write_bytes(b"x" * 100)
    target = root / "dst.bin"

    original_open = MemPath.open

    class _Exploding:
        def __init__(self, inner):
            self._inner = inner
            self._reads = 0

        def read(self, size=-1):
            self._reads += 1
            if self._reads > 1:
                raise OSError("connection reset")
            return self._inner.read(10)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()

    def fake_open(self, mode="r", *args, **kwargs):
        handle = original_open(self, mode, *args, **kwargs)
        if self == src and "r" in mode:
            return _Exploding(handle)
        return handle

    monkeypatch.setattr(MemPath, "open", fake_open)
    with pytest.raises(OSError, match="connection reset"):
        src.copy(target)
    monkeypatch.undo()
    assert not target.exists()
    assert src.read_bytes() == b"x" * 100


@pytest.mark.parametrize("make_path", ["local", "mem"])
def test_copy_onto_itself_raises_and_keeps_content(tmp_path, make_path):
    if make_path == "local":
        path = LocalPath(tmp_path / "f.txt")
    else:
        path = MemPath("/") / "f.txt"
    path.write_text("DATA")
    with pytest.raises(OSError, match="same file"):
        path.copy(path, overwrite=True)
    assert path.read_text() == "DATA"


def test_copy_onto_case_alias_raises_on_case_insensitive_fs(tmp_path):
    path = LocalPath(tmp_path / "f.txt")
    path.write_text("DATA")
    alias = LocalPath(tmp_path / "F.TXT")
    if not alias.exists():
        pytest.skip("case-sensitive filesystem")
    with pytest.raises(OSError, match="same file"):
        path.copy(alias, overwrite=True)
    assert path.read_text() == "DATA"


def test_same_file_guard_ignores_equal_paths_on_different_mem_backends():
    # Equal segments on two separate in-memory filesystems are two files.
    a = MemPath("/") / "f.txt"
    b = MemPath("/") / "f.txt"
    a.write_text("A")
    b.write_text("B")
    a.copy(b, overwrite=True)
    assert b.read_text() == "A"
    assert a.read_text() == "A"


# --- move -----------------------------------------------------------------


def test_move_missing_source_does_not_remove_target_tree(tmp_path):
    target = LocalPath(tmp_path / "precious")
    target.mkdir()
    (target / "keep.txt").write_text("K")
    with pytest.raises(FileNotFoundError):
        LocalPath(tmp_path / "missing").move(target, overwrite=True)
    assert (target / "keep.txt").read_text() == "K"


def test_move_file_onto_directory_refuses_and_keeps_both(tmp_path):
    target = LocalPath(tmp_path / "precious")
    target.mkdir()
    (target / "keep.txt").write_text("K")
    src = LocalPath(tmp_path / "f.txt")
    src.write_text("F")
    with pytest.raises(IsADirectoryError):
        src.move(target, overwrite=True)
    assert (target / "keep.txt").read_text() == "K"
    assert src.read_text() == "F"


def test_move_file_over_file_replaces_it(tmp_path):
    src = LocalPath(tmp_path / "new.txt")
    src.write_text("NEW")
    target = LocalPath(tmp_path / "old.txt")
    target.write_text("OLD")
    src.move(target, overwrite=True)
    assert target.read_text() == "NEW"
    assert not src.exists()


def test_case_only_move_renames_instead_of_deleting(tmp_path):
    src = LocalPath(tmp_path / "readme.txt")
    src.write_text("R")
    target = LocalPath(tmp_path / "README.txt")
    if not target.exists():
        pytest.skip("case-sensitive filesystem")
    src.move(target, overwrite=True)
    assert os.listdir(tmp_path) == ["README.txt"]
    assert target.read_text() == "R"


def test_move_onto_itself_keeps_the_file(tmp_path):
    path = LocalPath(tmp_path / "x.txt")
    path.write_text("X")
    path.move(path, overwrite=True)
    assert path.read_text() == "X"


def test_mem_move_over_existing_file_replaces_it():
    root = MemPath("/")
    src = root / "a.txt"
    src.write_text("A")
    target = root / "b.txt"
    target.write_text("B")
    src.move(target, overwrite=True)
    assert target.read_text() == "A"
    assert not src.exists()


# --- move and copy: one file under several names -------------------------


@pytest.fixture(params=["local", "mem"])
def disk(request, tmp_path):
    """An empty directory on the local disk, and one in memory."""
    return LocalPath(tmp_path) if request.param == "local" else MemPath("/")


def _nested(root):
    """`root/dst`, holding `other.txt` and `sub/data.txt`."""
    dst = root / "dst"
    (dst / "sub").mkdir(parents=True)
    (dst / "other.txt").write_text("OTHER")
    (dst / "sub" / "data.txt").write_text("DATA")
    return dst


def _both_trees_survive(dst):
    assert (dst / "other.txt").read_text() == "OTHER"
    assert (dst / "sub" / "data.txt").read_text() == "DATA"


def _tolerate_refusal(operation):
    """Whether the operation refused (`OSError`) or did nothing is not what
    these tests are about; what survives is."""
    try:
        operation()
    except OSError:
        pass


def _symlink_or_skip(link, target, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")


def test_move_directory_onto_its_ancestor_refuses_and_keeps_both_trees(disk):
    dst = _nested(disk)
    with pytest.raises(OSError):
        (dst / "sub").move(dst, overwrite=True)
    _both_trees_survive(dst)


def test_move_directory_onto_its_ancestor_without_overwrite_raises_file_exists(disk):
    dst = _nested(disk)
    with pytest.raises(FileExistsError):
        (dst / "sub").move(dst)
    _both_trees_survive(dst)


def test_move_directory_into_itself_refuses_and_keeps_the_tree(disk):
    dst = _nested(disk)
    with pytest.raises(OSError):
        dst.move(dst / "sub", overwrite=True)
    _both_trees_survive(dst)
    with pytest.raises(OSError):
        dst.move(dst / "sub" / "new")
    assert not (dst / "sub" / "new").exists()
    _both_trees_survive(dst)


def test_move_directory_onto_itself_keeps_the_tree(disk):
    dst = _nested(disk)
    _tolerate_refusal(lambda: dst.move(dst, overwrite=True))
    _both_trees_survive(dst)


def test_move_directory_onto_a_spelling_of_itself_keeps_the_tree(disk):
    dst = _nested(disk)
    _tolerate_refusal(lambda: dst.move(dst / "sub" / "..", overwrite=True))
    _both_trees_survive(dst)


def test_move_directory_onto_a_case_alias_keeps_the_tree(tmp_path):
    src = LocalPath(tmp_path / "readme")
    src.mkdir()
    (src / "data.txt").write_text("DATA")
    alias = LocalPath(tmp_path / "README")
    if not alias.exists():
        pytest.skip("case-sensitive filesystem")
    _tolerate_refusal(lambda: src.move(alias, overwrite=True))
    assert (alias / "data.txt").read_text() == "DATA"


def test_move_onto_the_same_directory_through_a_symlinked_parent_keeps_it(tmp_path):
    real = tmp_path / "real"
    (real / "dst").mkdir(parents=True)
    (real / "dst" / "data.txt").write_text("DATA")
    _symlink_or_skip(tmp_path / "alias", real, directory=True)
    _tolerate_refusal(
        lambda: LocalPath(real / "dst").move(
            LocalPath(tmp_path / "alias" / "dst"), overwrite=True
        )
    )
    assert (real / "dst" / "data.txt").read_text() == "DATA"


def _as_another_class(kind, path):
    """The file `path` names, as an instance of another class."""
    if kind == "subclass":

        class SubLocal(LocalPath):
            __slots__ = ()

        return SubLocal(path)
    if kind == "stdlib":
        return pathlib.Path(path)
    pytest.importorskip("uritools")
    from pathlib_next.uri import UriPath

    return UriPath(path)


@pytest.mark.parametrize("kind", ["subclass", "stdlib", "file-uri"])
def test_move_onto_the_same_file_under_another_class_keeps_it(tmp_path, kind):
    path = LocalPath(tmp_path / "f.txt")
    path.write_text("PRECIOUS")
    _tolerate_refusal(lambda: path.move(_as_another_class(kind, path), overwrite=True))
    assert path.read_text() == "PRECIOUS"


@pytest.mark.parametrize("kind", ["subclass", "file-uri"])
def test_move_directory_onto_the_same_directory_under_another_class_keeps_it(
    tmp_path, kind
):
    dst = _nested(LocalPath(tmp_path))
    _tolerate_refusal(lambda: dst.move(_as_another_class(kind, dst), overwrite=True))
    _both_trees_survive(dst)


@pytest.mark.parametrize("kind", ["subclass", "file-uri"])
def test_copy_onto_the_same_file_under_another_class_raises_and_keeps_it(
    tmp_path, kind
):
    path = LocalPath(tmp_path / "f.txt")
    path.write_text("PRECIOUS")
    with pytest.raises(OSError, match="same file"):
        path.copy(_as_another_class(kind, path), overwrite=True)
    assert path.read_text() == "PRECIOUS"


def _hard_link_or_skip(existing, new):
    try:
        os.link(existing, new)
    except (OSError, NotImplementedError, AttributeError) as error:
        pytest.skip(f"hard link unavailable: {error}")


def test_move_between_two_hard_links_needs_overwrite(tmp_path):
    first = LocalPath(tmp_path / "a.txt")
    first.write_text("A")
    _hard_link_or_skip(first, tmp_path / "b.txt")
    with pytest.raises(FileExistsError):
        first.move(LocalPath(tmp_path / "b.txt"))
    assert sorted(os.listdir(tmp_path)) == ["a.txt", "b.txt"]


def test_move_between_two_hard_links_leaves_the_target_name(tmp_path):
    first = LocalPath(tmp_path / "a.txt")
    first.write_text("A")
    _hard_link_or_skip(first, tmp_path / "b.txt")
    second = LocalPath(tmp_path / "b.txt")
    first.move(second, overwrite=True)
    assert sorted(os.listdir(tmp_path)) == ["b.txt"]
    assert second.read_text() == "A"


def test_move_a_link_onto_the_file_it_points_at_keeps_the_file(tmp_path):
    path = LocalPath(tmp_path / "f.txt")
    path.write_text("PRECIOUS")
    _symlink_or_skip(tmp_path / "link", path)
    with pytest.raises(OSError):
        LocalPath(tmp_path / "link").move(path, overwrite=True)
    assert path.read_text() == "PRECIOUS"
    assert os.path.islink(tmp_path / "link")


def test_mem_move_onto_the_same_file_spelled_relative_keeps_it():
    root = MemPath("/")
    (root / "a.txt").write_text("DATA")
    relative = MemPath("a.txt", backend=root.backend)
    _tolerate_refusal(lambda: relative.move(root / "a.txt", overwrite=True))
    _tolerate_refusal(lambda: (root / "a.txt").move(relative, overwrite=True))
    assert (root / "a.txt").read_text() == "DATA"


def test_mem_move_onto_the_same_file_through_dot_dot_keeps_it():
    root = MemPath("/")
    (root / "d").mkdir()
    (root / "a.txt").write_text("DATA")
    _tolerate_refusal(
        lambda: (root / "a.txt").move(root / "d" / ".." / "a.txt", overwrite=True)
    )
    assert (root / "a.txt").read_text() == "DATA"


def test_mem_copy_onto_the_same_file_spelled_relative_raises_and_keeps_it():
    root = MemPath("/")
    (root / "a.txt").write_text("DATA")
    relative = MemPath("a.txt", backend=root.backend)
    with pytest.raises(OSError, match="same file"):
        relative.copy(root / "a.txt", overwrite=True)
    assert (root / "a.txt").read_text() == "DATA"


def test_mem_copy_directory_into_a_relative_spelling_of_itself_refuses():
    root = MemPath("/")
    dst = _nested(root)
    inside = MemPath("dst/new", backend=root.backend)
    with pytest.raises(OSError, match="into itself"):
        dst.copy(inside, recursive=True)
    assert not (dst / "new").exists()


def test_copy_directory_into_itself_through_a_symlinked_parent_refuses(tmp_path):
    real = tmp_path / "real"
    (real / "dst").mkdir(parents=True)
    (real / "dst" / "data.txt").write_text("DATA")
    _symlink_or_skip(tmp_path / "alias", real, directory=True)
    with pytest.raises(OSError, match="into itself"):
        LocalPath(real / "dst").copy(
            LocalPath(tmp_path / "alias" / "dst" / "new"), recursive=True
        )
    assert not (real / "dst" / "new").exists()


def test_copy_overwrite_asks_the_target_to_unlink_with_missing_ok():
    """A backend whose write replaces the file cannot delete: its `unlink()`
    returns when told a missing target is fine, and the copy still lands."""
    calls = []

    class ReplaceOnWrite(MemPath):
        __slots__ = ()

        def unlink(self, missing_ok=False):
            calls.append(missing_ok)
            if not missing_ok:
                raise NotImplementedError("a write replaces the file")

    root = ReplaceOnWrite("/")
    (root / "src.txt").write_text("NEW")
    (root / "dst.txt").write_text("OLD")
    (root / "src.txt").copy(root / "dst.txt", overwrite=True)
    assert (root / "dst.txt").read_text() == "NEW"
    assert calls == [True]


# --- rm through links -----------------------------------------------------


def _junction_or_skip(link, target):
    if sys.platform != "win32":
        pytest.skip("junctions are Windows-only")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not os.path.lexists(link):
        pytest.skip(f"could not create junction: {result.stderr.strip()}")


def _victim(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("V")
    return victim


def test_rm_recursive_does_not_descend_into_junction(tmp_path):
    victim = _victim(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    _junction_or_skip(tree / "j", victim)
    LocalPath(tree).rm(recursive=True)
    assert not tree.exists()
    assert (victim / "keep.txt").read_text() == "V"


def test_rm_recursive_on_junction_itself_removes_only_the_link(tmp_path):
    victim = _victim(tmp_path)
    link = tmp_path / "j"
    _junction_or_skip(link, victim)
    LocalPath(link).rm(recursive=True)
    assert not os.path.lexists(link)
    assert (victim / "keep.txt").read_text() == "V"


def test_file_uri_rm_recursive_does_not_descend_into_junction(tmp_path):
    pytest.importorskip("uritools")
    from pathlib_next.uri import UriPath

    victim = _victim(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    _junction_or_skip(tree / "j", victim)
    UriPath(LocalPath(tree)).rm(recursive=True)
    assert not tree.exists()
    assert (victim / "keep.txt").read_text() == "V"


def test_file_uri_rm_recursive_does_not_follow_directory_symlink(tmp_path):
    pytest.importorskip("uritools")
    from pathlib_next.uri import UriPath

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("P")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("a")
    try:
        os.symlink(outside, tree / "link", target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory symlink unavailable: {error}")

    UriPath(LocalPath(tree)).rm(recursive=True)

    assert (outside / "precious.txt").read_text() == "P"
    assert not os.path.lexists(tree / "link")


def test_file_uri_scandir_reports_symlink_not_directory(tmp_path):
    pytest.importorskip("uritools")
    from pathlib_next.uri import UriPath

    (tmp_path / "real").mkdir()
    try:
        os.symlink(tmp_path / "real", tmp_path / "link", target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory symlink unavailable: {error}")

    stats = dict(UriPath(LocalPath(tmp_path))._scandir())
    assert stats["link"].is_symlink()
    assert not stats["link"].is_dir()
    assert stats["real"].is_dir()


# --- untrusted child names ------------------------------------------------


@pytest.mark.parametrize(
    "name", ["", ".", "..", "a/b", "../x", "/abs", "nul\0byte", None, 3]
)
def test_unsafe_child_names_rejected_on_every_flavour(name):
    from pathlib_next.utils import is_safe_child_name

    assert not is_safe_child_name(name)
    assert not is_safe_child_name(name, windows=True)


@pytest.mark.parametrize(
    "name",
    [
        "D:evil.txt",
        "C:..",
        "a\\..\\b",
        "x:stream",
        ".. ",
        ". ",
        # Windows drops a trailing dot or space: "report." is the file "report".
        "name.",
        "report.",
        "a ",
        "x. .",
        "...",
        "trail. ",
        # A reserved device name, in any case, with or without an extension.
        "NUL",
        "nul",
        "Con",
        "PRN",
        "aux",
        "COM1",
        "com9",
        "LPT1",
        "lpt9",
        "nul.txt",
        "NUL.tar.gz",
        "COM1.log",
        "nul .txt",
    ],
)
def test_windows_only_unsafe_child_names(name):
    from pathlib_next.utils import is_safe_child_name

    assert is_safe_child_name(name)
    assert not is_safe_child_name(name, windows=True)


@pytest.mark.parametrize(
    "name",
    [
        "a.txt",
        ".hidden",
        "12-00.log",
        "...x",
        # Resembling a device name is not being one.
        "COM0",
        "COM10",
        "LPT0",
        "console",
        "null",
        "nullable",
        "auxiliary.txt",
        ".nul",
        "a.nul",
        "com1x",
        " nul",
        "CONIN$",
    ],
)
def test_ordinary_child_names_accepted(name):
    from pathlib_next.utils import is_safe_child_name

    assert is_safe_child_name(name)
    assert is_safe_child_name(name, windows=True)


def test_is_windows_flavoured_matches_path_semantics(tmp_path):
    from pathlib import PurePosixPath, PureWindowsPath

    from pathlib_next.utils import is_windows_flavoured

    assert is_windows_flavoured(PureWindowsPath("C:/x"))
    assert not is_windows_flavoured(PurePosixPath("/x"))
    assert not is_windows_flavoured(MemPath("/"))
    assert is_windows_flavoured(LocalPath(tmp_path)) == (os.name == "nt")


# --- a binding is not a symlink -------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows")
def test_a_junction_is_a_binding_not_a_symlink(tmp_path):
    """A junction is a second NAME for a directory -- the Windows spelling
    of a bind mount -- so `is_symlink()` is False and a non-following stat
    calls it an ordinary directory. That is exactly why a symlink check
    cannot protect a recursive walk from one."""
    import _winapi

    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "keep.txt").write_text("PRECIOUS")
    _winapi.CreateJunction(str(tmp_path / "real"), str(tmp_path / "bind"))
    (tmp_path / "plain").mkdir()

    binding = LocalPath(tmp_path / "bind")
    assert binding.is_symlink() is False
    assert binding.is_junction() is True
    assert binding.is_dir_binding() is True
    # A non-following stat still calls it a directory -- no link in sight.
    assert binding.is_dir()

    for name in ("real", "plain"):
        ordinary = LocalPath(tmp_path / name)
        assert ordinary.is_junction() is False
        assert ordinary.is_dir_binding() is False


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows")
def test_recursive_rm_removes_the_binding_not_the_tree_behind_it(tmp_path):
    import _winapi

    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "keep.txt").write_text("PRECIOUS")
    (tmp_path / "tree").mkdir()
    _winapi.CreateJunction(str(tmp_path / "real"), str(tmp_path / "tree" / "bind"))
    (tmp_path / "tree" / "own.txt").write_text("mine")

    LocalPath(tmp_path / "tree").rm(recursive=True)

    assert not (tmp_path / "tree").exists()
    # What the junction pointed at is untouched.
    assert (tmp_path / "real" / "keep.txt").read_text() == "PRECIOUS"


def test_recursive_rm_does_not_descend_into_a_mount_point(tmp_path, monkeypatch):
    """The POSIX half: a bind mount announces itself through `is_mount()`
    and nothing else -- no link, and a stat that says "directory". Deleting
    its contents would delete the mounted filesystem's, so `rm` removes the
    mount point itself, which fails loudly on a live mount instead."""
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "mounted").mkdir()
    (tmp_path / "tree" / "mounted" / "theirs.txt").write_text("NOT MINE")
    (tmp_path / "tree" / "own.txt").write_text("mine")

    real_is_mount = LocalPath.is_mount

    def fake_is_mount(self):
        return self.name == "mounted" or real_is_mount(self)

    monkeypatch.setattr(LocalPath, "is_mount", fake_is_mount)

    errors = []

    def tolerate(error, path):
        errors.append((error, path))
        return True  # the callable's return value decides; None re-raises

    LocalPath(tmp_path / "tree").rm(recursive=True, ignore_error=tolerate)

    # rmdir() on a non-empty mount point fails, which is the point: the
    # content behind it is still there, and the failure was reported rather
    # than the mounted filesystem being emptied.
    assert (tmp_path / "tree" / "mounted" / "theirs.txt").read_text() == "NOT MINE"
    assert any(isinstance(error, OSError) for error, _path in errors)
    # The tree's own file was still removed.
    assert not (tmp_path / "tree" / "own.txt").exists()


# --- rm(follow_symlinks=, follow_binds=) ----------------------------------


@pytest.fixture
def tree_with_link_and_binding(tmp_path):
    """A tree holding one symlink, and on Windows one binding too, both
    pointing at a directory OUTSIDE the tree, plus a file of its own."""
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "keep.txt").write_text("PRECIOUS")
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "own.txt").write_text("mine")
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(
            str(tmp_path / "target"), str(tmp_path / "tree" / "bind")
        )
    # A bind mount needs privileges, so the binding half is Windows-only;
    # `is_mount()` covers the POSIX side elsewhere.
    _symlink_or_skip(tmp_path / "tree" / "link", tmp_path / "target", directory=True)
    return tmp_path


#: What `tree_with_link_and_binding` puts besides `own.txt` in `tree`.
LINK_NAMES = ["bind", "link"] if os.name == "nt" else ["link"]


def _tolerate(errors):
    def handler(error, path):
        errors.append((type(error).__name__, path.name))
        return True

    return handler


def test_rm_removes_a_link_and_a_binding_not_what_is_behind_them(
    tree_with_link_and_binding,
):
    """The default, and what `rm -r` does: the entry goes, its target does
    not."""
    root = tree_with_link_and_binding
    LocalPath(root / "tree").rm(recursive=True)
    assert not (root / "tree").exists()
    assert (root / "target" / "keep.txt").read_text() == "PRECIOUS"


def test_rm_follow_removes_what_is_behind_them(tree_with_link_and_binding):
    """Opt in and the contents behind both go too -- what a walker that
    cannot tell a binding from a directory does by accident."""
    root = tree_with_link_and_binding
    LocalPath(root / "tree").rm(recursive=True, follow_symlinks=True, follow_binds=True)
    assert not (root / "tree").exists()
    assert not (root / "target" / "keep.txt").exists()


def test_rm_ignore_leaves_them_in_place(tree_with_link_and_binding):
    """`ignore` does not remove the entry either, so the enclosing directory
    is not empty and says so through `ignore_error`."""
    root = tree_with_link_and_binding
    errors = []
    LocalPath(root / "tree").rm(
        recursive=True,
        ignore_error=_tolerate(errors),
        follow_symlinks=None,
        follow_binds=None,
    )
    left = sorted(p.name for p in (root / "tree").iterdir())
    assert left == LINK_NAMES
    assert (root / "target" / "keep.txt").read_text() == "PRECIOUS"
    assert errors  # the tree's own rmdir reported the leftovers
    assert not (root / "tree" / "own.txt").exists()


def test_rm_policy_may_be_decided_per_entry(tree_with_link_and_binding):
    """A callable is asked per entry, so one tree can keep one binding and
    remove another."""
    root = tree_with_link_and_binding
    LocalPath(root / "tree").rm(
        recursive=True,
        ignore_error=True,
        follow_symlinks=lambda path: False,
        follow_binds=lambda path: None if path.name == "bind" else False,
    )
    if os.name == "nt":
        assert (root / "tree" / "bind").exists()
    assert not (root / "tree" / "link").exists()
    assert (root / "target" / "keep.txt").read_text() == "PRECIOUS"


@pytest.mark.parametrize("keyword", ["follow_symlinks", "follow_binds"])
def test_rm_rejects_an_unknown_policy(tmp_path, keyword):
    (tmp_path / "d").mkdir()
    with pytest.raises(ValueError, match="True, False, None or a callable"):
        LocalPath(tmp_path / "d").rm(recursive=True, **{keyword: "delete-everything"})


@pytest.mark.parametrize("keyword", ["follow_symlinks", "follow_binds"])
def test_rm_rejects_an_unknown_policy_before_removing_anything(tmp_path, keyword):
    """A policy that is not a policy is a caller error: `ignore_error` is
    for removal failures and must not hide it."""
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("x")
    with pytest.raises(ValueError):
        LocalPath(tmp_path / "d").rm(
            recursive=True, ignore_error=True, **{keyword: "delete-everything"}
        )
    assert (tmp_path / "d" / "f.txt").read_text() == "x"


def test_rm_policy_answer_that_is_not_a_policy_is_raised_not_ignored(
    tree_with_link_and_binding,
):
    root = tree_with_link_and_binding
    with pytest.raises(ValueError, match="True, False, None or a callable"):
        LocalPath(root / "tree").rm(
            recursive=True, ignore_error=True, follow_symlinks=lambda path: 1
        )
    assert (root / "target" / "keep.txt").read_text() == "PRECIOUS"
    with pytest.raises(ValueError, match="True, False, None or a callable"):
        LocalPath(root / "tree" / "link").rm(
            recursive=True, ignore_error=True, follow_symlinks=lambda path: "yes"
        )
    assert (root / "target" / "keep.txt").read_text() == "PRECIOUS"


def test_rm_follow_symlinks_removes_the_link_after_what_is_behind_it(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("P")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "keep.txt").write_text("k")
    _symlink_or_skip(tree / "link", outside, directory=True)

    LocalPath(tree).rm(recursive=True, follow_symlinks=True)

    assert not tree.exists()
    assert outside.is_dir()
    assert os.listdir(outside) == []


def _tree_with_one_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("P")
    link = tmp_path / "link"
    _symlink_or_skip(link, outside, directory=True)
    return LocalPath(link), outside


@pytest.mark.parametrize(
    "policy, link_left, content_left",
    [
        (False, False, True),
        (True, False, False),
        (None, True, True),
        (lambda path: True, False, False),
        (lambda path: None, True, True),
    ],
    ids=["False", "True", "None", "callable-True", "callable-None"],
)
def test_rm_of_a_symlink_named_directly_applies_follow_symlinks(
    tmp_path, policy, link_left, content_left
):
    """The entry the caller names is decided by the same policy as one met
    on the way down."""
    link, outside = _tree_with_one_link(tmp_path)

    link.rm(recursive=True, follow_symlinks=policy)

    assert os.path.lexists(link) is link_left
    assert (outside / "precious.txt").exists() is content_left
    assert outside.is_dir()


def test_rm_of_a_symlink_without_recursion_removes_only_the_link(tmp_path):
    link, outside = _tree_with_one_link(tmp_path)
    link.rm(follow_symlinks=True)
    assert not os.path.lexists(link)
    assert (outside / "precious.txt").read_text() == "P"


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows")
def test_rm_of_a_binding_named_directly_removes_the_binding(tmp_path):
    """Naming the binding itself removes it, not the tree behind it --
    `rm -r` semantics for the path the caller gave."""
    import _winapi

    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "keep.txt").write_text("PRECIOUS")
    _winapi.CreateJunction(str(tmp_path / "target"), str(tmp_path / "bind"))

    LocalPath(tmp_path / "bind").rm(recursive=True)
    assert not (tmp_path / "bind").exists()
    assert (tmp_path / "target" / "keep.txt").read_text() == "PRECIOUS"


# --- names only Windows rewrites ------------------------------------------


@pytest.fixture
def windows_flavoured(monkeypatch):
    """Every path counts as one read with Windows rules, so the rule runs on
    any platform against `MemPath`."""
    from pathlib_next import utils

    monkeypatch.setattr(utils, "is_windows_flavoured", lambda path: True)


def _refused(errors):
    return sorted(str(error) for error in errors if isinstance(error, ValueError))


def test_copy_recursive_never_merges_names_windows_rewrites(windows_flavoured):
    source = MemPath("/rewrite-src")
    source.mkdir()
    for name in ("report", "report.", "a ", "nul", "Con.txt", "ok.txt"):
        (source / name).write_text(f"data:{name}")
    target = MemPath("/rewrite-dst")
    errors = []

    source.copy(target, recursive=True, ignore_error=errors.append)

    assert sorted(child.name for child in target.iterdir()) == ["ok.txt", "report"]
    assert (target / "report").read_text() == "data:report"
    refused = _refused(errors)
    assert len(refused) == 4
    for name in ("report.", "a ", "nul", "Con.txt"):
        assert any(repr(name) in message for message in refused), name


def test_sync_never_merges_names_windows_rewrites(windows_flavoured):
    source = MemPath("/rewrite-sync-src")
    source.mkdir()
    for name in ("report", "report.", "nul", "ok.txt"):
        (source / name).write_text(f"data:{name}")
    target = MemPath("/rewrite-sync-dst")
    errors = []

    PathSyncer(
        lambda entry: entry.stat.st_size,
        ignore_error=lambda error, *args: errors.append(error) or True,
    ).sync(source, target)

    assert sorted(child.name for child in target.iterdir()) == ["ok.txt", "report"]
    assert (target / "report").read_text() == "data:report"
    assert len(_refused(errors)) == 2


def test_rm_recursive_removes_names_its_own_listing_returned(windows_flavoured):
    # The Windows rewrite rule is for a name arriving from elsewhere; a name a
    # directory listed itself is removed like any other (`aux.c` is a file).
    tree = MemPath("/rewrite-rm")
    tree.mkdir()
    for name in ("ok", "aux.c", "trail."):
        (tree / name).write_text(name)

    tree.rm(recursive=True)

    assert not tree.exists()


@pytest.mark.skipif(os.name != "nt", reason="needs a Windows file system")
def test_copy_recursive_to_a_windows_disk_keeps_report_apart_from_report_dot(tmp_path):
    source = MemPath("/rewrite-disk-src")
    source.mkdir()
    (source / "report").write_text("kept")
    (source / "report.").write_text("dot")
    (source / "NUL").write_text("nul")
    dest = tmp_path / "dest"
    errors = []

    source.copy(
        pathlib_next.LocalPath(dest),
        recursive=True,
        overwrite=True,
        ignore_error=errors.append,
    )

    assert sorted(os.listdir(dest)) == ["report"]
    assert (dest / "report").read_text() == "kept"
    assert len(_refused(errors)) == 2
