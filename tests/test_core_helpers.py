"""`utils.LRU`, `as_mode()`, `as_owner()`, `parsedate()` and `FileStat`."""

from __future__ import annotations

import stat

import pytest

import pathlib_next
from pathlib_next import utils
from pathlib_next.utils.stat import FileStat

# --- LRU ------------------------------------------------------------------


def test_lru_with_maxsize_zero_stores_nothing_and_evicts_nothing():
    evicted = []
    calls = []

    def make(key):
        calls.append(key)
        return [key]

    cache = utils.LRU(
        make, maxsize=0, on_evict=lambda key, value: evicted.append(value)
    )
    first = cache(1)
    second = cache(1)
    assert calls == [1, 1]
    assert first is not second
    assert evicted == []
    assert len(cache.cache) == 0


def test_lru_with_a_negative_maxsize_stores_nothing():
    cache = utils.LRU(lambda key: [key], maxsize=-3)
    cache(1)
    assert len(cache.cache) == 0


def test_lru_with_maxsize_none_is_unbounded():
    cache = utils.LRU(lambda key: [key], maxsize=None)
    values = [cache(index) for index in range(500)]
    assert len(cache.cache) == 500
    assert cache(7) is values[7]


def test_lru_maxsize_can_be_set_to_zero_and_none():
    evicted = []
    cache = utils.LRU(
        lambda key: [key], maxsize=4, on_evict=lambda key, value: evicted.append(key)
    )
    for index in range(3):
        cache(index)
    cache.maxsize = 0
    assert len(cache.cache) == 0
    assert sorted(evicted) == [(0,), (1,), (2,)]
    cache.maxsize = None
    cache(9)
    assert len(cache.cache) == 1


def test_lru_still_evicts_the_oldest_over_a_positive_maxsize():
    evicted = []
    cache = utils.LRU(
        lambda key: [key], maxsize=2, on_evict=lambda key, value: evicted.append(key)
    )
    for index in range(3):
        cache(index)
    assert evicted == [(0,)]


# --- as_mode / as_owner ---------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 0o200000, 1 << 40, "07777777777777", "-1"])
def test_as_mode_refuses_a_number_that_cannot_be_a_mode(bad):
    with pytest.raises(ValueError):
        utils.as_mode(bad)


@pytest.mark.parametrize("bad", [True, False])
def test_as_mode_refuses_a_bool(bad):
    with pytest.raises(TypeError):
        utils.as_mode(bad)


@pytest.mark.parametrize(
    "good,expected",
    [(0, 0), (0o644, 0o644), ("0755", 0o755), (0o7777, 0o7777), (0o100644, 0o100644)],
)
def test_as_mode_keeps_a_mode_and_the_type_bits_of_a_st_mode(good, expected):
    assert utils.as_mode(good) == expected


def test_a_whole_st_mode_still_goes_through_chmod(tmp_path):
    target = pathlib_next.LocalPath(tmp_path / "f")
    target.write_text("x")
    target.chmod(target.stat().st_mode)
    target.chmod(stat.S_IMODE(target.stat().st_mode))


@pytest.mark.parametrize("bad", [(True, None), (None, False), (True, False)])
def test_as_owner_refuses_a_bool(bad):
    with pytest.raises(TypeError):
        utils.as_owner(*bad)


@pytest.mark.parametrize("bad", [(-2, None), (None, -7), (1 << 32, None)])
def test_as_owner_refuses_an_id_that_cannot_exist(bad):
    with pytest.raises(ValueError):
        utils.as_owner(*bad)


def test_as_owner_keeps_ids_names_and_the_unchanged_sentinels():
    assert utils.as_owner(-1, 5) == (None, 5)
    assert utils.as_owner(0, 0) == (0, 0)
    assert utils.as_owner("root", None) == ("root", None)
    assert utils.as_owner((1 << 32) - 1, -1) == ((1 << 32) - 1, None)


# --- parsedate ------------------------------------------------------------


def test_parsedate_states_what_it_accepts_and_what_it_does_not():
    text = " ".join((utils.parsedate.__doc__ or "").split())
    assert "Nothing else is accepted" in text
    for word in ("bytes", "datetime", "fewer than six"):
        assert word in text
    assert "31 Feb" in text


# --- FileStat -------------------------------------------------------------


def test_a_bare_permission_mode_gets_the_type_of_the_file():
    assert FileStat(st_mode=0o644).is_file()
    assert not FileStat(st_mode=0o644).is_dir()
    assert FileStat(st_mode=0o755, is_dir=True).is_dir()
    assert not FileStat(st_mode=0o755, is_dir=True).is_file()
    assert stat.S_IMODE(FileStat(st_mode=0o755, is_dir=True).st_mode) == 0o755


def test_a_mode_that_has_a_type_keeps_it():
    assert FileStat(st_mode=stat.S_IFLNK | 0o777).is_symlink()
    assert FileStat(st_mode=stat.S_IFREG | 0o600, is_dir=True).is_file()
    assert FileStat(st_mode=stat.S_IFDIR | 0o700).is_dir()


def test_a_permission_mode_counts_as_reported():
    assert FileStat(st_mode=0o644).mode_known is True
    assert FileStat().mode_known is False
    assert FileStat(is_dir=True).is_dir()


def test_setmode_keeps_a_device_or_socket_from_becoming_a_directory():
    for kind in (stat.S_IFBLK, stat.S_IFSOCK, stat.S_IFCHR):
        listed = FileStat(st_mode=kind | 0o600)
        listed.setmode(0o644)
        assert listed.is_file()
        assert not listed.is_dir()
    directory = FileStat(is_dir=True)
    directory.setmode(0o700)
    assert directory.is_dir()
    assert stat.S_IMODE(directory.st_mode) == 0o700
