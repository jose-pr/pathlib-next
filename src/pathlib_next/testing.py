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


class PathContract:
    """Mixin of filesystem-contract tests every Path implementation
    (custom `Path` subclass, or `UriPath` scheme) must satisfy.

    Subclass this with a `root` fixture that returns an empty, writable
    directory-like instance of your Path implementation. See
    `pathlib_next`'s own `tests/test_contract.py` for a worked example
    running this against LocalPath/MemPath/FileUri.
    """

    def test_mkdir_and_is_dir(self, root):
        d = root / "d"
        assert not d.exists()
        d.mkdir()
        assert d.is_dir()
        assert not d.is_file()

    def test_write_read_text_roundtrip(self, root):
        f = root / "f.txt"
        f.write_text("hello")
        assert f.exists()
        assert f.is_file()
        assert not f.is_dir()
        assert f.read_text() == "hello"

    def test_write_read_bytes_roundtrip(self, root):
        f = root / "f.bin"
        f.write_bytes(b"\x00\x01hello")
        assert f.read_bytes() == b"\x00\x01hello"

    def test_iterdir_lists_children(self, root):
        (root / "a.txt").write_text("a")
        (root / "b.txt").write_text("b")
        (root / "sub").mkdir()
        names = {p.name for p in root.iterdir()}
        assert names == {"a.txt", "b.txt", "sub"}

    def test_unlink(self, root):
        f = root / "f.txt"
        f.write_text("x")
        f.unlink()
        assert not f.exists()

    def test_unlink_missing_raises_then_missing_ok(self, root):
        f = root / "missing.txt"
        with pytest.raises(FileNotFoundError):
            f.unlink()
        f.unlink(missing_ok=True)

    def test_rmdir_requires_empty(self, root):
        d = root / "d"
        d.mkdir()
        (d / "f.txt").write_text("x")
        with pytest.raises(OSError):
            d.rmdir()
        (d / "f.txt").unlink()
        d.rmdir()
        assert not d.exists()

    def test_rm_recursive(self, root):
        d = root / "d"
        d.mkdir()
        (d / "f.txt").write_text("x")
        (d / "sub").mkdir()
        (d / "sub" / "g.txt").write_text("y")
        d.rm(recursive=True)
        assert not d.exists()

    def test_rm_missing_ok(self, root):
        with pytest.raises(FileNotFoundError):
            (root / "missing").rm()
        (root / "missing").rm(missing_ok=True)

    def test_copy_preserves_source(self, root):
        src = root / "src.txt"
        src.write_text("data")
        dst = root / "dst.txt"
        src.copy(dst)
        assert dst.read_text() == "data"
        assert src.exists()

    def test_copy_existing_target_raises_without_overwrite(self, root):
        src = root / "src.txt"
        src.write_text("data")
        dst = root / "dst.txt"
        dst.write_text("existing")
        with pytest.raises(FileExistsError):
            src.copy(dst)
        src.copy(dst, overwrite=True)
        assert dst.read_text() == "data"

    def test_move(self, root):
        src = root / "src.txt"
        src.write_text("data")
        dst = root / "dst.txt"
        src.move(dst)
        assert not src.exists()
        assert dst.read_text() == "data"

    def test_exists_false_for_missing(self, root):
        assert not (root / "nope").exists()

    def test_touch_exist_ok_false_raises_without_truncating(self, root):
        f = root / "f.txt"
        f.write_text("keep")
        with pytest.raises(FileExistsError):
            f.touch(exist_ok=False)
        assert f.read_text() == "keep"

    def test_mkdir_parents(self, root):
        d = root / "a" / "b" / "c"
        d.mkdir(parents=True)
        assert d.is_dir()
        with pytest.raises(FileExistsError):
            d.mkdir(parents=True, exist_ok=False)
        d.mkdir(parents=True, exist_ok=True)
