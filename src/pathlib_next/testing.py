"""Test-suite building blocks for verifying custom Path/UriPath
implementations satisfy the library's filesystem contract.

Not imported by `pathlib_next/__init__.py` -- this module requires pytest,
which is a test-only dependency. Import it explicitly:

    from pathlib_next.testing import PathContract

    class TestMyPath(PathContract):
        @pytest.fixture
        def root(self, tmp_path):
            return MyPath(tmp_path)
"""
import pytest


class PurePathContract:
    """Contract tests for pure path (logical) operations, not requiring any I/O."""

    def test_pure_basics(self, root):
        p = root / "dir" / "file.txt"
        assert p.name == "file.txt"
        assert p.suffix == ".txt"
        assert p.stem == "file"
        
        # parent and parents
        assert p.parent.name == "dir"
        assert len(p.parents) >= 2
        assert p.parents[0].name == "dir"

    def test_pure_joinpath_truediv(self, root):
        p = root / "a"
        assert (p / "b").name == "b"
        assert p.joinpath("b", "c").name == "c"

    def test_pure_match(self, root):
        p = root / "dir" / "file.txt"
        assert p.match("*.txt")
        assert not p.match("*.py")


class ReadPathContract(PurePathContract):
    """Contract tests for read-only path operations.

    Subclasses must provide a `root` fixture pointing to a directory pre-populated
    with the standard `fixture_tree` (see conftest.py):
    - a.txt (content: "a")
    - b.py (content: "b")
    - .hidden.txt (content: "hidden")
    - sub/c.py (content: "c")
    - sub/nested/d.py (content: "d")
    - empty_dir/
    """

    def test_exists_and_types(self, root):
        assert root.exists()
        assert root.is_dir()
        
        a = root / "a.txt"
        assert a.exists()
        assert a.is_file()
        assert not a.is_dir()

        sub = root / "sub"
        assert sub.exists()
        assert sub.is_dir()
        assert not sub.is_file()

        assert not (root / "nonexistent").exists()

    def test_read_text_and_bytes(self, root):
        assert (root / "a.txt").read_text() == "a"
        assert (root / "a.txt").read_bytes() == b"a"
        assert (root / "sub" / "c.py").read_text() == "c"

    def test_iterdir_lists_children(self, root):
        try:
            names = {p.name for p in root.iterdir()}
        except (NotImplementedError, NotADirectoryError):
            # e.g. DataUri or single-file paths do not support directory listing
            return
        assert "a.txt" in names
        assert "sub" in names
        assert "empty_dir" in names

    def test_stat(self, root):
        st = (root / "a.txt").stat()
        assert st.st_size == 1


class PathContract(ReadPathContract):
    """Mixin of filesystem-contract tests every writable Path implementation
    (custom `Path` subclass, or `UriPath` scheme) must satisfy.

    Subclasses must provide a `root` fixture pointing to a writable directory
    pre-populated with the standard `fixture_tree` (see conftest.py).
    
    Note: ftp/dav/s3 stay mock-unit-only because their hand-rolled fakes
    aren't faithful enough for a generic contract.
    """

    def test_mkdir_and_is_dir(self, root):
        d = root / "new_dir"
        assert not d.exists()
        d.mkdir()
        assert d.is_dir()
        assert not d.is_file()

    def test_write_read_text_roundtrip(self, root):
        f = root / "write_f.txt"
        f.write_text("hello")
        assert f.exists()
        assert f.is_file()
        assert not f.is_dir()
        assert f.read_text() == "hello"

    def test_write_read_bytes_roundtrip(self, root):
        f = root / "write_f.bin"
        f.write_bytes(b"\x00\x01hello")
        assert f.read_bytes() == b"\x00\x01hello"

    def test_unlink(self, root):
        f = root / "write_unlink.txt"
        f.write_text("x")
        assert f.exists()
        f.unlink()
        assert not f.exists()

    def test_unlink_missing_raises_then_missing_ok(self, root):
        f = root / "missing_unlink.txt"
        with pytest.raises(FileNotFoundError):
            f.unlink()
        f.unlink(missing_ok=True)

    def test_rmdir_requires_empty(self, root):
        d = root / "new_rmdir"
        d.mkdir()
        (d / "f.txt").write_text("x")
        with pytest.raises(OSError):
            d.rmdir()
        (d / "f.txt").unlink()
        d.rmdir()
        assert not d.exists()

    def test_rm_recursive(self, root):
        d = root / "new_rm_rec"
        d.mkdir()
        (d / "f.txt").write_text("x")
        (d / "sub_rec").mkdir()
        (d / "sub_rec" / "g.txt").write_text("y")
        d.rm(recursive=True)
        assert not d.exists()

    def test_rm_missing_ok(self, root):
        with pytest.raises(FileNotFoundError):
            (root / "missing_rm").rm()
        (root / "missing_rm").rm(missing_ok=True)

    def test_copy_preserves_source(self, root):
        src = root / "a.txt"
        dst = root / "dst_copy.txt"
        src.copy(dst)
        assert dst.read_text() == "a"
        assert src.exists()

    def test_copy_existing_target_raises_without_overwrite(self, root):
        src = root / "a.txt"
        dst = root / "dst_copy_existing.txt"
        dst.write_text("existing")
        with pytest.raises(FileExistsError):
            src.copy(dst)
        src.copy(dst, overwrite=True)
        assert dst.read_text() == "a"

    def test_move(self, root):
        # We write a temp file to move so we don't destroy a.txt for other tests
        src = root / "src_move.txt"
        src.write_text("data")
        dst = root / "dst_move.txt"
        src.move(dst)
        assert not src.exists()
        assert dst.read_text() == "data"

    def test_touch_exist_ok_false_raises_without_truncating(self, root):
        f = root / "touch_f.txt"
        f.write_text("keep")
        with pytest.raises(FileExistsError):
            f.touch(exist_ok=False)
        assert f.read_text() == "keep"

    def test_mkdir_parents(self, root):
        d = root / "parent_a" / "parent_b" / "parent_c"
        d.mkdir(parents=True)
        assert d.is_dir()
        with pytest.raises(FileExistsError):
            d.mkdir(parents=True, exist_ok=False)
        d.mkdir(parents=True, exist_ok=True)
