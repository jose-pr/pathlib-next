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
