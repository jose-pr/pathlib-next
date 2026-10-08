import copy
import errno
import pickle

import pytest

from pathlib_next.mempath import MemFile, MemPath, MemPathBackend


def test_backend_shared_across_joins_from_empty_root():
    # Regression: MemPath.__init__ used `if _backend and backend is None:`
    # (dict truthiness) to decide whether to propagate a parent's backend --
    # an empty (but valid) backend dict is falsy, so joining off a fresh
    # MemPath silently gave the child a disconnected new backend.
    root = MemPath("/")
    child = root / "a.txt"
    assert child.backend is root.backend
    child.write_text("x")
    assert "a.txt" in {p.name for p in root.iterdir()}


def test_backend_shared_explicit():
    backend = MemPathBackend()
    a = MemPath("/a", backend=backend)
    b = MemPath("/b", backend=backend)
    a.write_text("data")
    assert b.parent.joinpath("a").read_text() == "data"


def test_stat_missing_raises_b4():
    with pytest.raises(FileNotFoundError):
        MemPath("/missing").stat()


def test_stat_dir_vs_file():
    root = MemPath("/")
    (root / "d").mkdir()
    (root / "f.txt").write_text("x")
    assert (root / "d").stat().is_dir()
    assert (root / "f.txt").stat().is_dir() is False


# --- B20: mode dispatch ---


def test_open_mode_r_missing_raises():
    with pytest.raises(FileNotFoundError):
        MemPath("/missing.txt")._open("r")


def test_open_mode_w_truncates_existing():
    root = MemPath("/")
    f = root / "f.txt"
    f.write_text("original content")
    f.write_text("new")
    assert f.read_text() == "new"


def test_open_mode_x_raises_if_exists():
    root = MemPath("/")
    f = root / "f.txt"
    f.write_text("data")
    with pytest.raises(FileExistsError):
        f._open("x")


def test_open_mode_x_creates_new():
    root = MemPath("/")
    f = root / "new.txt"
    with f._open("x") as fh:
        fh.write(b"hi")
    assert f.read_text() == "hi"


def test_open_mode_a_appends_to_existing():
    root = MemPath("/")
    f = root / "f.txt"
    f.write_text("abc")
    with f.open("a") as fh:
        fh.write("def")
    assert f.read_text() == "abcdef"


def test_open_mode_a_creates_if_missing():
    root = MemPath("/")
    f = root / "new.txt"
    with f.open("a") as fh:
        fh.write("x")
    assert f.read_text() == "x"


def test_open_unsupported_mode_raises_notimplemented():
    root = MemPath("/")
    (root / "f.txt").write_text("x")
    with pytest.raises(NotImplementedError):
        (root / "f.txt")._open("r+")


def test_open_r_on_directory_raises():
    root = MemPath("/")
    (root / "d").mkdir()
    with pytest.raises(IsADirectoryError):
        (root / "d")._open("r")


def test_membytesio_close_preserves_content_after_seek():
    # Regression: MemBytesIO.close() used seek(0);read() instead of
    # getvalue(), which lost content if the caller's cursor wasn't already
    # at position 0 when closing.
    root = MemPath("/")
    f = root / "f.txt"
    with f.open("wb") as fh:
        fh.write(b"hello world")
        fh.seek(3)  # cursor not at 0, and not at EOF, when closed
    assert f.read_bytes() == b"hello world"


# --- B21: normalization of ".."-escaping paths ---


def test_normalized_dotdot_clamps_at_root():
    assert MemPath("..").normalized == [""]
    assert MemPath("../../x").normalized == ["x"]


def test_normalized_root_no_double_slash():
    # Regression introduced by the B21 fix itself: prepending "/" to an
    # already-absolute posix ("/") produced "//", which posixpath.normpath
    # treats specially (POSIX double-slash root) instead of collapsing.
    assert MemPath("/").normalized == [""]


def test_normalized_regular_path():
    assert MemPath("a/b/../c").normalized == ["a", "c"]


# --- rmdir / unlink type errors ---


def test_unlink_on_directory_raises_isadirectoryerror():
    root = MemPath("/")
    (root / "d").mkdir()
    with pytest.raises(IsADirectoryError):
        (root / "d").unlink()


def test_rmdir_on_file_raises_notadirectoryerror():
    root = MemPath("/")
    (root / "f.txt").write_text("x")
    with pytest.raises(NotADirectoryError):
        (root / "f.txt").rmdir()


def test_rmdir_nonempty_raises():
    root = MemPath("/")
    (root / "d").mkdir()
    (root / "d" / "f.txt").write_text("x")
    with pytest.raises(OSError) as info:
        (root / "d").rmdir()
    assert info.value.errno == errno.ENOTEMPTY
    assert not isinstance(info.value, FileExistsError)


# --- 2026-08-16: open("w") over a directory destroyed the whole subtree ---


def test_open_w_on_directory_raises_and_keeps_the_tree():
    backend = MemPathBackend()
    root = MemPath("/", backend=backend)
    (root / "dir").mkdir()
    (root / "dir" / "child.txt").write_text("hi")

    with pytest.raises(IsADirectoryError):
        MemPath("dir", backend=backend).write_text("clobber")

    # The whole point of the fix: the tree survives the refused write.
    assert isinstance(backend["dir"], dict)
    assert (root / "dir" / "child.txt").read_text() == "hi"


def test_open_w_on_root_raises_instead_of_creating_an_empty_key():
    backend = MemPathBackend()
    with pytest.raises(IsADirectoryError):
        MemPath("/", backend=backend).write_text("clobber")
    assert backend == {}


def test_open_a_on_root_raises_instead_of_creating_an_empty_key():
    backend = MemPathBackend()
    with pytest.raises(IsADirectoryError):
        MemPath("/", backend=backend)._open("a")
    assert backend == {}


def test_open_w_still_truncates_an_existing_file():
    # Guard against over-correcting: only directories are refused.
    root = MemPath("/")
    (root / "f.txt").write_text("original")
    (root / "f.txt").write_text("new")
    assert (root / "f.txt").read_text() == "new"


# --- 2026-08-16: a path routed *through* a file raised TypeError ---


def test_exists_through_a_file_segment_is_false():
    backend = MemPathBackend()
    MemPath("file.txt", backend=backend).write_text("x")
    # Used to raise TypeError: a bytes-like object is required, not 'str'.
    assert MemPath("file.txt/sub", backend=backend).exists() is False
    assert MemPath("file.txt/sub/deeper", backend=backend).exists() is False
    assert MemPath("file.txt/sub", backend=backend).is_dir() is False


def test_stat_through_a_file_segment_raises_notadirectoryerror():
    backend = MemPathBackend()
    MemPath("file.txt", backend=backend).write_text("x")
    with pytest.raises(NotADirectoryError):
        MemPath("file.txt/sub", backend=backend).stat()


def test_open_through_a_file_segment_raises_notadirectoryerror():
    backend = MemPathBackend()
    MemPath("file.txt", backend=backend).write_text("x")
    with pytest.raises(NotADirectoryError):
        MemPath("file.txt/sub", backend=backend).read_text()


def test_mkdir_parents_under_a_file_raises_notadirectoryerror():
    backend = MemPathBackend()
    MemPath("file.txt", backend=backend).write_text("x")
    with pytest.raises(NotADirectoryError):
        MemPath("file.txt/sub", backend=backend).mkdir(parents=True)


def test_notadirectoryerror_names_the_offending_ancestor():
    backend = MemPathBackend()
    MemPath("a", backend=backend).mkdir()
    MemPath("a/f.txt", backend=backend).write_text("x")
    with pytest.raises(NotADirectoryError) as excinfo:
        MemPath("a/f.txt/sub", backend=backend).stat()
    assert excinfo.value.filename == "a/f.txt"
    assert excinfo.value.errno == errno.ENOTDIR


def test_iterdir_on_missing_path_raises_filenotfounderror():
    # pathlib raises FileNotFoundError; this used to be NotADirectoryError.
    with pytest.raises(FileNotFoundError):
        list(MemPath("/nope").iterdir())
    errors = []
    list(MemPath("/missing").walk(on_error=errors.append))
    assert [type(error) for error in errors] == [FileNotFoundError]


def test_iterdir_on_file_still_raises_notadirectoryerror():
    path = MemPath("/f.txt")
    path.write_text("x")
    with pytest.raises(NotADirectoryError):
        list(path.iterdir())


@pytest.mark.parametrize("write_mode, read_mode", [("wt", "rt"), ("w", "r")])
def test_text_modes_with_and_without_t(write_mode, read_mode):
    path = MemPath("/t.txt")
    with path.open(write_mode) as handle:
        handle.write("line\n")
    with path.open(read_mode) as handle:
        assert handle.read() == "line\n"
    created = path.with_name("x.txt")
    with created.open("xt") as handle:
        handle.write("new")
    assert created.read_text() == "new"


def test_mtime_is_set_and_advances_on_every_write():
    path = MemPath("/m.txt")
    path.write_bytes(b"old.")
    first = path.stat().st_mtime
    assert first > 0
    # Same size, new content: a (size, mtime) quick check must see it.
    path.write_bytes(b"NEW!")
    second = path.stat().st_mtime
    assert second > first
    with path.open("ab") as handle:
        handle.write(b"+")
    third = path.stat().st_mtime
    assert third > second
    # Reading does not touch it.
    path.read_bytes()
    assert path.stat().st_mtime == third


# --- a join names the left operand's filesystem ---


def test_joining_a_mempath_stays_on_the_left_operands_backend():
    root = MemPath("/")
    (root / "f.txt").write_text("data")
    other = MemPath("f.txt")

    for joined in (root / other, root.joinpath(other), MemPath(root, other)):
        assert joined.backend is root.backend
        assert joined.as_posix() == "/f.txt"
        assert joined.read_text() == "data"


def test_an_explicit_backend_wins_over_the_mempath_arguments():
    first, second, chosen = MemPathBackend(), MemPathBackend(), MemPathBackend()
    a, b = MemPath("/a", backend=first), MemPath("b", backend=second)

    assert MemPath(a, b).backend is first
    assert MemPath(a, b, backend=chosen).backend is chosen
    assert MemPath("/x", b).backend is second


def test_a_string_prefix_joined_onto_a_mempath_keeps_its_backend():
    path = MemPath("sub")
    assert ("/root" / path).backend is path.backend


def test_glob_with_a_mempath_pattern_searches_the_root_dir():
    from pathlib_next.utils import glob

    src = MemPath("/src")
    src.mkdir()
    (src / "x.py").write_text("")
    (src / "y.txt").write_text("")

    assert list(glob.glob(MemPath("*.py"), root_dir=src)) == [src / "x.py"]


# --- ".." is applied to the tree ---


def _tree():
    root = MemPath("/")
    (root / "d").mkdir()
    (root / "a.txt").write_text("precious")
    return root


def test_dotdot_through_a_missing_directory_is_not_found():
    root = _tree()
    before = dict(root.backend)

    path = root / "missing" / ".." / "a.txt"
    assert path.exists() is False
    with pytest.raises(FileNotFoundError):
        path.read_text()
    with pytest.raises(FileNotFoundError):
        (root / "missing" / ".." / "new").mkdir()
    assert dict(root.backend) == before


def test_dotdot_through_a_file_is_not_a_directory():
    root = _tree()

    path = root / "a.txt" / ".." / "d"
    assert path.is_dir() is False
    with pytest.raises(NotADirectoryError) as info:
        path.stat()
    assert info.value.errno == errno.ENOTDIR


def test_dotdot_through_existing_directories_names_the_same_node():
    root = _tree()
    reference = root / "a.txt"

    for spelling in ("/d/../a.txt", "d/../a.txt", "/../a.txt", "/d/./../a.txt"):
        path = MemPath(spelling, backend=root.backend)
        assert path._node_key() == reference._node_key()
        assert path.read_text() == "precious"


def test_a_trailing_dotdot_is_the_directory_above():
    root = _tree()
    (root / "d" / "e").mkdir()

    assert (root / "d" / "e" / "..").stat().is_dir()
    assert sorted(p.name for p in (root / "d" / "e" / "..").iterdir()) == ["e"]
    assert sorted(p.name for p in (root / "d" / "..").iterdir()) == ["a.txt", "d"]


def test_dotdot_does_not_climb_above_the_root():
    root = _tree()
    assert (root / ".." / ".." / "a.txt").read_text() == "precious"


def test_mkdir_parents_creates_the_directories_a_dotdot_passes_through():
    root = MemPath("/")
    (root / "made" / ".." / "new").mkdir(parents=True)
    assert sorted(root.backend) == ["made", "new"]


# --- appends and replacements are published as one step ---


def test_two_append_handles_both_land():
    path = MemPath("/log")
    path.write_bytes(b"0")
    first, second = path.open("ab"), path.open("ab")
    first.write(b"1")
    second.write(b"2")
    first.close()
    second.close()
    assert path.read_bytes() == b"012"


def test_append_handles_publish_only_what_they_wrote_on_flush():
    path = MemPath("/log")
    path.write_bytes(b"0")
    first, second = path.open("ab"), path.open("ab")
    first.write(b"1")
    first.flush()
    second.write(b"2")
    second.flush()
    first.write(b"3")
    first.close()
    second.close()
    assert path.read_bytes() == b"0123"


def test_text_append_handles_both_land():
    path = MemPath("/log")
    path.write_text("a")
    first, second = path.open("a"), path.open("a")
    first.write("b")
    second.write("c")
    first.close()
    second.close()
    assert path.read_text() == "abc"


def test_an_append_after_the_file_was_replaced_lands_at_its_new_end():
    path = MemPath("/log")
    path.write_bytes(b"old content")
    handle = path.open("ab")
    path.write_bytes(b"new")
    handle.write(b"+")
    handle.close()
    assert path.read_bytes() == b"new+"


class _WatchedFile(MemFile):
    """A file that counts how often it was emptied."""

    __slots__ = ("emptied",)

    def clear(self):
        self.emptied = getattr(self, "emptied", 0) + 1
        super().clear()


@pytest.mark.parametrize("mode", ["ab", "wb"])
def test_publishing_a_handle_never_empties_the_file(mode):
    # A reader between an emptying clear() and the extend() that follows
    # would see no content at all.
    backend = MemPathBackend()
    watched = _WatchedFile(b"x" * 100)
    backend["f"] = watched
    handle = MemPath("/f", backend=backend).open(mode)
    handle.write(b"tail")
    emptied = getattr(watched, "emptied", 0)

    handle.flush()
    handle.close()

    assert getattr(watched, "emptied", 0) == emptied
    assert bytes(watched).endswith(b"tail")


# --- the root cannot be removed ---


def test_rmdir_of_the_root_says_the_root_cannot_be_removed():
    root = _tree()
    with pytest.raises(OSError) as info:
        root.rmdir()
    assert info.value.errno == errno.EBUSY
    assert not isinstance(info.value, FileNotFoundError)
    assert "root" in str(info.value)


def test_recursive_rm_of_the_root_does_not_report_it_as_missing():
    root = _tree()
    with pytest.raises(OSError) as info:
        root.rm(recursive=True)
    assert info.value.errno == errno.EBUSY
    assert not isinstance(info.value, FileNotFoundError)


# --- segments are a value ---


def test_segments_are_an_immutable_tuple():
    path = MemPath("/a/b")
    assert path.segments == ("", "a", "b")
    assert isinstance(path.segments, tuple)
    assert isinstance(MemPath("a/b").segments, tuple)
    assert MemPath("").segments == ()
    assert MemPath("/").segments == ("", "")
    seen = {path}
    assert path in seen
    assert hash(path) == hash(MemPath("/a/b"))


def test_a_misspelled_keyword_is_refused():
    with pytest.raises(TypeError):
        MemPath("/", bakend=MemPathBackend())
    with pytest.raises(TypeError):
        MemPath("/", something=1)


# --- a file keeps its modification time through copy and pickle ---


def _stamped_backend():
    backend = MemPathBackend()
    MemPath("/d", backend=backend).mkdir()
    MemPath("/d/f.txt", backend=backend).write_text("v")
    backend["d"]["f.txt"].mtime = 1234.5
    return backend


@pytest.mark.parametrize("how", ["copy", "deepcopy", "pickle"])
def test_a_copied_file_keeps_its_mtime(how):
    file = _stamped_backend()["d"]["f.txt"]
    clone = {
        "copy": copy.copy,
        "deepcopy": copy.deepcopy,
        "pickle": lambda f: pickle.loads(pickle.dumps(f)),
    }[how](file)

    assert type(clone) is MemFile
    assert bytes(clone) == b"v"
    assert clone.mtime == 1234.5


@pytest.mark.parametrize("how", ["deepcopy", "pickle"])
def test_a_copied_backend_keeps_the_mtimes(how):
    backend = _stamped_backend()
    clone = (
        copy.deepcopy(backend)
        if how == "deepcopy"
        else pickle.loads(pickle.dumps(backend))
    )

    path = MemPath("/d/f.txt", backend=clone)
    assert path.read_text() == "v"
    assert path.stat().st_mtime == 1234.5
