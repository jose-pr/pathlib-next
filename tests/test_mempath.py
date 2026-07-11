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
    with pytest.raises(FileExistsError):
        (root / "d").rmdir()
