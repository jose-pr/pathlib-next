import calendar
import pathlib
import subprocess
import sys
import textwrap
import time

import pytest

from pathlib_next import utils


def test_lru_basic_cache_hit():
    calls = []

    def fn(x):
        calls.append(x)
        return x * 2

    lru = utils.LRU(fn)
    assert lru(2) == 4
    assert lru(2) == 4
    assert calls == [2]  # cached, fn only called once


def test_lru_invalidate_b5():
    # B5 regression: invalidate() did `with self.lock():` -- RLock isn't
    # callable, so this raised TypeError instead of invalidating.
    calls = []

    def fn(x):
        calls.append(x)
        return x * 2

    lru = utils.LRU(fn)
    lru(3)
    lru.invalidate(3)
    assert calls == [3, 3]  # recomputed after invalidation


def test_lru_maxsize_shrink_evicts_oldest_n1():
    # N1 regression: the maxsize setter called `cache.pop(last=False)` --
    # OrderedDict.pop() takes a key, not a `last` kwarg -- so shrinking
    # maxsize below the current fill raised TypeError instead of evicting.
    calls = []

    def fn(x):
        calls.append(x)
        return x * 2

    lru = utils.LRU(fn, maxsize=3)
    lru(1)
    lru(2)
    lru(3)
    lru.maxsize = 1
    assert lru.maxsize == 1
    assert list(lru.cache.keys()) == [(3,)]
    lru(3)
    assert calls == [1, 2, 3]  # 3 still cached, not recomputed


def test_sizeof_fmt_always_returns_str():
    # B-cleanup: small values used to return int, not str.
    assert isinstance(utils.sizeof_fmt(500), str)
    assert isinstance(utils.sizeof_fmt(5000), str)
    assert utils.sizeof_fmt(500) == "500"


def test_notimplemented_decorator_message():
    @utils.notimplemented
    def some_method(): ...

    try:
        some_method()
        assert False, "should have raised"
    except NotImplementedError as e:
        assert "some_method" in str(e)
        assert "  " not in str(e)  # no leftover double-space typo


def test_parsedate_none_returns_epoch_zero_b23():
    # B23 regression: parsedate(None) used to return time.time() ("now"),
    # poisoning freshness checks for e.g. HTTP responses with no
    # Last-Modified header.
    assert utils.parsedate(None) == 0


def test_parsedate_unparseable_string_returns_epoch_zero_b23():
    assert utils.parsedate("not a date") == 0


def test_parsedate_valid_string():
    # Exact UTC epoch seconds. Expected values come from calendar.timegm
    # (UTC by definition), so the machine's timezone cannot matter: the old
    # time.mktime() read the GMT date as local time, returning -25200 on a
    # UTC+7 host (OverflowError on Windows) and a loose range hid it.
    assert utils.parsedate("Thu, 01 Jan 1970 00:00:00 GMT") == 0


_OCT_21_2015_0728_UTC = calendar.timegm((2015, 10, 21, 7, 28, 0))


@pytest.mark.parametrize(
    "text",
    [
        "Wed, 21 Oct 2015 07:28:00 GMT",  # RFC 1123 (Last-Modified)
        "Wednesday, 21-Oct-15 07:28:00 GMT",  # RFC 850
        "Wed Oct 21 07:28:00 2015",  # asctime: no zone, GMT per HTTP
        "Wed, 21 Oct 2015 09:28:00 +0200",  # offset east of UTC
        "Wed, 21 Oct 2015 02:28:00 -0500",  # offset west of UTC
    ],
)
def test_parsedate_string_is_utc_epoch_seconds(text):
    assert _OCT_21_2015_0728_UTC == 1445412480
    assert utils.parsedate(text) == _OCT_21_2015_0728_UTC


def test_parsedate_numbers_pass_through():
    assert utils.parsedate(1445412480) == 1445412480
    assert utils.parsedate(1445412480.5) == 1445412480.5
    assert utils.parsedate(0) == 0


def test_parsedate_struct_time_without_zone_is_utc():
    zoneless = time.strptime("2015-10-21 07:28:00", "%Y-%m-%d %H:%M:%S")
    assert utils.parsedate(zoneless) == _OCT_21_2015_0728_UTC
    assert utils.parsedate((2015, 10, 21, 7, 28, 0, 0, 0, 0)) == 1445412480
    # Near the epoch: time.mktime raised OverflowError here on Windows east
    # of UTC.
    assert utils.parsedate(time.gmtime(0)) == 0


def test_parsedate_tuple_with_offset_applies_it():
    # A parsedate_tz-style 10-tuple carries its offset in seconds.
    assert utils.parsedate((2015, 10, 21, 9, 28, 0, 0, 1, -1, 7200)) == 1445412480


def test_sizeof_fmt_large_units():
    assert utils.sizeof_fmt(1024) == "1.0K"
    assert utils.sizeof_fmt(1024 * 1024) == "1.0M"
    assert utils.sizeof_fmt(1024 * 1024 * 1024) == "1.0G"
    assert utils.sizeof_fmt(1024**8) == "1.0Y"


def test_filestat_methods():
    from pathlib_next.utils.stat import FileStat

    # test __init__, settime, setmode
    st = FileStat(is_dir=False, st_size=100, st_mtime=50)
    assert st.st_size == 100
    assert st.st_mtime == 50
    assert st.is_file()
    assert not st.is_dir()

    st.settime(123)
    assert st.st_atime == 123
    assert st.st_mtime == 123
    assert st.st_ctime == 123

    st.setmode(0o755, isdir=False)
    import stat

    assert stat.S_ISREG(st.st_mode)
    assert (st.st_mode & 0o777) == 0o755

    st.setmode(0o700, isdir=True)
    assert stat.S_ISDIR(st.st_mode)
    assert (st.st_mode & 0o777) == 0o700

    # __getitem__, items, __repr__, __str__
    assert st["st_size"] == 100
    items_dict = dict(st.items())
    assert items_dict["st_size"] == 100

    rep = repr(st)
    assert "FileStat" in rep

    s = str(st)
    assert "st_size=100" in s

    # from_path with FileStat returned from path.stat()
    class FakePathWithFileStat:
        def stat(self, follow_symlinks=True):
            return st

    assert FileStat.from_path(FakePathWithFileStat()) is st

    # from_path with missing raises None
    class FakePath:
        def stat(self, follow_symlinks=True):
            raise FileNotFoundError()

    assert FileStat.from_path(FakePath()) is None

    # from_path with normal stat object
    class FakeStat:
        def __init__(self):
            self.st_mode = 0o100644
            self.st_size = 50
            self.st_mtime = 10

    class FakePathWithStat:
        def stat(self, follow_symlinks=True):
            return FakeStat()

    st_from = FileStat.from_path(FakePathWithStat())
    assert st_from.st_size == 50
    assert st_from.st_mtime == 10


def test_stat_protocol_helpers():
    from pathlib_next.protocols.fs import Stat
    import stat

    class StubStatPath(Stat):
        def __init__(self, mode):
            self.mode = mode

        def stat(self, *, follow_symlinks=True):
            class FakeStat:
                st_mode = self.mode
                st_size = 0
                st_mtime = 0

            return FakeStat()

    # Block device
    p_blk = StubStatPath(stat.S_IFBLK)
    assert p_blk.is_block_device()
    assert not p_blk.is_char_device()
    assert not p_blk.is_fifo()
    assert not p_blk.is_socket()

    # Character device
    p_chr = StubStatPath(stat.S_IFCHR)
    assert not p_chr.is_block_device()
    assert p_chr.is_char_device()
    assert not p_chr.is_fifo()
    assert not p_chr.is_socket()

    # FIFO
    p_fifo = StubStatPath(stat.S_IFIFO)
    assert not p_fifo.is_block_device()
    assert not p_fifo.is_char_device()
    assert p_fifo.is_fifo()
    assert not p_fifo.is_socket()

    # Socket
    p_sock = StubStatPath(stat.S_IFSOCK)
    assert not p_sock.is_block_device()
    assert not p_sock.is_char_device()
    assert not p_sock.is_fifo()
    assert p_sock.is_socket()

    # Test error fallback to None st_mode returning False
    class ErrStatPath(Stat):
        def stat(self, *, follow_symlinks=True):
            raise PermissionError("Access denied")

    p_err = ErrStatPath()
    assert not p_err.exists()
    assert not p_err.is_dir()
    assert not p_err.is_file()
    assert not p_err.is_symlink()


def test_checksum_helpers(tmp_path):
    from pathlib_next import LocalPath
    from pathlib_next.utils import md5, sha256
    import hashlib

    p = LocalPath(tmp_path / "test.txt")
    p.write_bytes(b"hello world")

    expected_md5 = hashlib.md5(b"hello world").hexdigest()
    expected_sha256 = hashlib.sha256(b"hello world").hexdigest()

    assert md5(p) == expected_md5
    assert sha256(p) == expected_sha256


def test_archive_helpers(tmp_path):
    from pathlib_next import LocalPath
    from pathlib_next.utils import make_archive, unpack_archive
    from pathlib_next.mempath import MemPath

    # 1. LocalPath to LocalPath
    src = LocalPath(tmp_path / "src")
    src.mkdir()
    (src / "file1.txt").write_text("hello")
    (src / "sub").mkdir()
    (src / "sub" / "file2.txt").write_text("world")

    for fmt in ("zip", "tar"):
        archive_path = LocalPath(tmp_path / f"archive.{fmt}")
        make_archive(src, fmt, archive_path)
        assert archive_path.exists()

        dest = LocalPath(tmp_path / f"dest_{fmt}")
        unpack_archive(archive_path, dest)
        assert (dest / "file1.txt").read_text() == "hello"
        assert (dest / "sub" / "file2.txt").read_text() == "world"

    # 2. Scheme-agnostic check: MemPath as destination
    mem_dest = MemPath("/extracted")
    zip_archive = LocalPath(tmp_path / "archive.zip")
    unpack_archive(zip_archive, mem_dest)
    assert (mem_dest / "file1.txt").read_text() == "hello"
    assert (mem_dest / "sub" / "file2.txt").read_text() == "world"


@pytest.mark.parametrize(
    "name, peek, expected",
    [
        ("a.zip", b"", "zip"),
        ("a.jar", b"", "zip"),
        ("A.ZIP", b"", "zip"),
        ("a.tar", b"", "tar"),
        ("a.tar.gz", b"", "tar"),
        ("a.tgz", b"", "tar"),
        ("a.tar.bz2", b"", "tar"),
        ("a.tar.xz", b"", "tar"),
        ("noext", b"PK\x03\x04", "zip"),
        ("noext", b"\x1f\x8b", "tar"),
    ],
)
def test_detect_format(name, peek, expected):
    from pathlib_next.utils.archive import _detect_format

    assert _detect_format(name, lambda: peek) == expected


def test_detect_format_does_not_peek_when_extension_is_conclusive():
    from pathlib_next.utils.archive import _detect_format

    def _boom():
        raise AssertionError("peek() should not be called for a conclusive extension")

    assert _detect_format("a.zip", _boom) == "zip"
    assert _detect_format("a.tar", _boom) == "tar"


@pytest.mark.skipif(
    sys.version_info >= (3, 10),
    reason="the ParamSpec fallback is only reachable on 3.9 (3.10+ has typing.ParamSpec)",
)
def test_paramspec_fallback_importable_without_typing_extensions():
    # Regression: on Python 3.9 `typing.ParamSpec` does not exist, so the module
    # falls back to `typing_extensions.ParamSpec` and, failing that, to a local
    # shim. The old fallback was a bare `TypeVar`, which has no `.args`, so the
    # `*args: K.args` annotations raised `AttributeError: 'TypeVar' object has no
    # attribute 'args'` at import. It went unnoticed because every dev/CI env had
    # `typing_extensions` installed transitively -- but it is not a runtime
    # dependency, so a clean 3.9 install of the package could not import it.
    #
    # 3.9-only by nature, not by convenience: `Generic[K, V]` on 3.9 rejects a
    # plain object ("Parameters to generic types must be types"), so the shim must
    # subclass `TypeVar` -- and `TypeVar` stopped being subclassable in 3.12. The
    # branch is unreachable on 3.10+ anyway, since `typing.ParamSpec` exists there.
    #
    # Runs in a subprocess with `typing_extensions` blocked so the fallback is
    # exercised for real rather than mocked.
    probe = textwrap.dedent("""
        import builtins

        _real_import = builtins.__import__

        def _blocked(name, *a, **k):
            if name == "typing_extensions":
                raise ImportError("emulating an env without typing_extensions")
            return _real_import(name, *a, **k)

        builtins.__import__ = _blocked

        from pathlib_next.utils import LRU, K

        assert K.args is not None, "ParamSpec fallback lost .args"
        assert K.kwargs is not None, "ParamSpec fallback lost .kwargs"
        lru = LRU(lambda x: x * 2, maxsize=4)
        assert lru(21) == 42
        assert lru(21) == 42
        lru.invalidate(21)
        assert lru(21) == 42
        print("FALLBACK_OK")
        """)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(utils.__file__).parents[3],
    )
    assert (
        "FALLBACK_OK" in result.stdout
    ), f"fallback import failed:\nstdout={result.stdout}\nstderr={result.stderr}"
