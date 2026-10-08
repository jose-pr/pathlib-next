"""What a sync that changes nothing costs a backend, counted per call."""

import collections
import stat as _stat

import pytest

from pathlib_next.mempath import MemPath
from pathlib_next.utils.stat import FileStat
from pathlib_next.utils.sync import PathSyncer, SyncEvent

DIRECTORIES = 3
FILES_PER_DIRECTORY = 4


class _Listed(MemPath):
    """A path whose listing carries the stats, as SFTP, S3 and HTTP do, and
    which counts `stat()` and listing calls. Each subclass counts apart."""

    calls: collections.Counter

    def stat(self, *, follow_symlinks=True):
        type(self).calls["stat"] += 1
        return super().stat(follow_symlinks=follow_symlinks)

    def _scandir(self):
        type(self).calls["scan"] += 1
        parent, name = self._parent_container()
        for key, value in list((parent[name] if name else parent).items()):
            yield key, self._listed(key, value)

    def _listed(self, name, value):
        is_dir = isinstance(value, dict)
        return FileStat(
            is_dir=is_dir,
            st_size=0 if is_dir else len(value),
            st_mtime=getattr(value, "mtime", 0),
        )


class _Source(_Listed):
    calls = collections.Counter()


class _Target(_Listed):
    calls = collections.Counter()


class _SourceWithoutStats(_Source):
    """Lists names only: every stat is unknown, never missing."""

    calls = collections.Counter()

    def _listed(self, name, value):
        return None


class _TargetWithoutStats(_Target):
    calls = collections.Counter()

    def _listed(self, name, value):
        return None


def _populate(source):
    source.mkdir()
    for directory in range(DIRECTORIES - 1):
        (source / f"d{directory}").mkdir()
        for file in range(FILES_PER_DIRECTORY):
            (source / f"d{directory}" / f"f{file}").write_bytes(b"x" * (file + 1))
    for file in range(FILES_PER_DIRECTORY):
        (source / f"top{file}").write_bytes(b"y" * (file + 1))


def _tree(root):
    return {
        path.as_posix(): path.read_bytes() if path.is_file() else None
        for path in _walk(root)
    }


def _walk(root):
    for child in root.iterdir():
        yield child
        if child.is_dir():
            yield from _walk(child)


def _reset(*kinds):
    for kind in kinds:
        kind.calls.clear()


@pytest.mark.parametrize("remove_missing", [False, True])
@pytest.mark.parametrize("follow_symlinks", [True, False])
def test_a_sync_that_changes_nothing_lists_once_and_stats_no_entry(
    remove_missing, follow_symlinks
):
    source, target = _Source("/s"), _Target("/t")
    _populate(source)
    syncer = PathSyncer(remove_missing=remove_missing, follow_symlinks=follow_symlinks)
    syncer.sync(source, target)
    assert len(_tree(target)) == len(_tree(source))

    events = []
    _reset(_Source, _Target)
    PathSyncer(
        remove_missing=remove_missing,
        follow_symlinks=follow_symlinks,
        hook=lambda s, t, event, dry_run: events.append(event),
    ).sync(source, target)

    # The two roots are stat'd once each; every other answer is in a listing.
    assert _Source.calls == {"stat": 1, "scan": DIRECTORIES}
    assert _Target.calls == {"stat": 1, "scan": DIRECTORIES}
    assert SyncEvent.Copy not in events
    assert SyncEvent.RemovedMissing not in events


def test_a_changed_file_is_copied_and_the_rest_still_cost_nothing():
    source, target = _Source("/s"), _Target("/t")
    _populate(source)
    PathSyncer().sync(source, target)
    (source / "d0" / "f1").write_bytes(b"changed!")

    copied = []
    _reset(_Source, _Target)
    PathSyncer(
        hook=lambda s, t, event, dry_run: (
            copied.append(t.path.as_posix()) if event is SyncEvent.Copy else None
        )
    ).sync(source, target)

    assert copied == ["/t/d0/f1"]
    assert (target / "d0" / "f1").read_bytes() == b"changed!"
    # The copy asks a few questions about the one file; the other twelve
    # entries cost nothing.
    assert _Source.calls["stat"] <= 3
    assert _Target.calls["stat"] <= 3


def test_remove_missing_still_removes_what_the_listing_reports():
    source, target = _Source("/s"), _Target("/t")
    _populate(source)
    PathSyncer(remove_missing=True).sync(source, target)
    (target / "extra.txt").write_bytes(b"extra")
    (target / "d0" / "gone").mkdir()

    PathSyncer(remove_missing=True).sync(source, target)

    assert not (target / "extra.txt").exists()
    assert not (target / "d0" / "gone").exists()
    assert (target / "d0" / "f0").exists()


def test_a_listing_without_stats_is_unknown_and_each_entry_is_stat_once():
    # A backend that lists names only pays for what it did not tell: one
    # `stat()` per entry on each side. None of them reads as "missing".
    source, target = _SourceWithoutStats("/s"), _TargetWithoutStats("/t")
    _populate(source)
    PathSyncer(remove_missing=True).sync(source, target)
    entries = len(_tree(source))

    events = []
    _reset(_SourceWithoutStats, _TargetWithoutStats)
    PathSyncer(
        remove_missing=True,
        hook=lambda s, t, event, dry_run: events.append(event),
    ).sync(source, target)

    assert SyncEvent.Copy not in events
    assert SyncEvent.RemovedMissing not in events
    assert _SourceWithoutStats.calls == {"stat": 1 + entries, "scan": DIRECTORIES}
    assert _TargetWithoutStats.calls == {"stat": 1 + entries, "scan": DIRECTORIES}
    assert len(_tree(target)) == entries


class _LinkingSource(_Source):
    """Lists the entry `link` as a symbolic link to the file `real`."""

    calls = collections.Counter()
    followed = []

    def _listed(self, name, value):
        if name == "link":
            return FileStat(st_mode=_stat.S_IFLNK | 0o777)
        return super()._listed(name, value)

    def stat(self, *, follow_symlinks=True):
        if self.name == "link":
            if not follow_symlinks:
                return FileStat(st_mode=_stat.S_IFLNK | 0o777)
            type(self).followed.append(self.name)
            return MemPath.stat(self.parent / "real")
        return super().stat(follow_symlinks=follow_symlinks)


def test_a_listed_link_is_stat_through_when_links_are_followed():
    source, target = _LinkingSource("/s"), _Target("/t")
    source.mkdir()
    (source / "real").write_bytes(b"content")
    (source / "link").write_bytes(b"not read")

    _LinkingSource.followed.clear()
    PathSyncer(follow_symlinks=True).sync(source, target)

    # The listing says link, so the sync asked what it resolves to: a file
    # to copy, not a link to recreate on a target that cannot hold one.
    assert "link" in _LinkingSource.followed
    assert (target / "real").read_bytes() == b"content"
    assert (target / "link").read_bytes() == b"not read"


class _NativeSource(_Source):
    """A source that can compute its own digest, and counts doing so."""

    calls = collections.Counter()

    def checksum(self, algorithm="md5"):
        type(self).calls["checksum"] += 1
        return "native:" + self.read_bytes().hex()

    def supported_checksums(self):
        return frozenset({"md5"})


def test_no_native_digest_is_computed_for_a_target_that_cannot_answer():
    source, target = _NativeSource("/s"), MemPath("/t")
    source.mkdir()
    for index in range(5):
        (source / f"f{index}").write_bytes(b"same")
    (source / "changed").write_bytes(b"new content")
    PathSyncer(quick_check=False).sync(source, target)
    (target / "changed").write_bytes(b"old content")

    _reset(_NativeSource)
    PathSyncer(quick_check=False).sync(source, target)

    assert _NativeSource.calls["checksum"] == 0
    assert (target / "changed").read_bytes() == b"new content"


class _NativeTarget(MemPath):
    log: list

    def checksum(self, algorithm="md5"):
        type(self).log.append(("target", self.name))
        return "native:" + self.read_bytes().hex()

    def supported_checksums(self):
        return frozenset({"md5"})


def test_the_target_is_not_asked_for_a_digest_the_source_cannot_give():
    class Refusing(_NativeSource):
        calls = collections.Counter()

        def checksum(self, algorithm="md5"):
            type(self).calls["checksum"] += 1
            raise NotImplementedError(algorithm)

    _NativeTarget.log = []
    source, target = Refusing("/s"), _NativeTarget("/t")
    source.mkdir()
    (source / "a").write_bytes(b"same")
    PathSyncer(quick_check=False).sync(source, target)
    (source / "a").write_bytes(b"other")

    _reset(Refusing)
    _NativeTarget.log.clear()
    PathSyncer(quick_check=False).sync(source, target)

    assert Refusing.calls["checksum"] == 1
    assert _NativeTarget.log == []
    assert (target / "a").read_bytes() == b"other"


def test_both_native_digests_are_used_when_both_sides_can_answer():
    class NativeSource(_NativeSource):
        calls = collections.Counter()

    _NativeTarget.log = []
    source, target = NativeSource("/s"), _NativeTarget("/t")
    source.mkdir()
    (source / "a").write_bytes(b"same")
    PathSyncer(quick_check=False).sync(source, target)

    _reset(NativeSource)
    _NativeTarget.log.clear()
    PathSyncer(quick_check=False).sync(source, target)

    assert NativeSource.calls["checksum"] == 1
    assert _NativeTarget.log == [("target", "a")]


def test_a_subclass_overrides_log_to_route_progress():
    messages = []

    class Quiet(PathSyncer):
        __slots__ = ()

        def log(self, msg, *args):
            messages.append(msg % args)

    source, target = MemPath("/s"), MemPath("/t")
    source.mkdir()
    (source / "a").write_bytes(b"1")
    Quiet().sync(source, target)

    assert any("Copy" in message for message in messages)
    with pytest.raises(AttributeError):
        PathSyncer().log = print
