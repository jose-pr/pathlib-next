import errno

import pytest

from pathlib_next.mempath import MemPath, MemPathBackend


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
    assert str(excinfo.value.args[0]) == "a/f.txt"


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
