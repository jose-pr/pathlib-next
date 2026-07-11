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
