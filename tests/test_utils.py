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
    def some_method():
        ...

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
    # mktime() interprets the parsed struct_time as local time, so this
    # isn't necessarily exactly epoch 0 -- just confirm it parses to a
    # real (non-epoch-zero, non-error) timestamp near 1970.
    result = utils.parsedate("Thu, 01 Jan 1970 00:00:00 GMT")
    assert 0 <= result < 24 * 3600


def test_sizeof_fmt_large_units():
    assert utils.sizeof_fmt(1024) == "1.0K"
    assert utils.sizeof_fmt(1024 * 1024) == "1.0M"
    assert utils.sizeof_fmt(1024 * 1024 * 1024) == "1.0G"
    assert utils.sizeof_fmt(1024**8) == "1.0Y"


def test_get_machine_ips():
    import socket
    import unittest.mock
    mock_getaddrinfo = [
        (socket.AddressFamily.AF_INET, None, None, None, ("192.168.1.5", 0)),
        (socket.AddressFamily.AF_INET6, None, None, None, ("fe80::1", 0, 0, 0)),
        (999, None, None, None, ("invalid", 0))  # Unknown protocol
    ]
    with unittest.mock.patch("socket.gethostname", return_value="myhost"), \
         unittest.mock.patch("socket.getaddrinfo", return_value=mock_getaddrinfo):
        utils.get_machine_ips.cache_clear()
        ips = utils.get_machine_ips()
        assert len(ips) == 2
        import ipaddress
        assert ipaddress.ip_address("192.168.1.5") in ips
        assert ipaddress.ip_address("fe80::1") in ips


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

