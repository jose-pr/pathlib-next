"""Regression tests for the optional backend-native checksum protocol
(`protocols/checksum.py::NativeChecksum`) and its wiring into
`utils/checksum.py`/`utils/sync.py::PathSyncer`. SFTP-specific native/
fallback coverage lives in `tests/test_sftp.py`, following that module's
`_FakeBackend` pattern.
"""

from pathlib_next.mempath import MemPath
from pathlib_next.protocols.checksum import NativeChecksum
from pathlib_next.utils import checksum as checksum_utils
from pathlib_next.utils.stat import FileStat
from pathlib_next.utils.sync import PathSyncer


class _NativeChecksumMemPath(MemPath, NativeChecksum):
    """Dummy `Path` subclass proving the protocol is preferred over the
    streaming fallback when present -- mirrors the "Track A: subclass Path
    directly" extension pattern (architecture.md), using MemPath as the
    concrete base like the module docstring's exemplar.

    `PathSyncer` re-derives its own path objects internally (`_children()`
    -> `entry.path / name`, going through `with_segments()`), so a call
    tracked on *this* Python object would never be visible on the syncer's
    internal copy. Call/config state is therefore keyed by posix path in a
    dict living on the (already tree-shared, see `MemPath.__init__`)
    backend -- every instance pointing at the same virtual file shares the
    same tracking entry, regardless of which object made the call.
    """

    __slots__ = ()

    def _tracking(self):
        store = self.backend.__dict__.setdefault("_native_checksum_test_state", {})
        return store.setdefault(
            self.as_posix(), {"checksum_calls": [], "open_calls": [], "forced": None}
        )

    @property
    def checksum_calls(self):
        return self._tracking()["checksum_calls"]

    @property
    def open_calls(self):
        return self._tracking()["open_calls"]

    def force_checksum(self, value):
        self._tracking()["forced"] = value
        return self

    def checksum(self, algorithm: str = "md5") -> str:
        state = self._tracking()
        state["checksum_calls"].append(algorithm)
        if state["forced"] is None:
            raise NotImplementedError("no forced checksum configured")
        return state["forced"]

    def supported_checksums(self):
        return (
            frozenset({"md5"})
            if self._tracking()["forced"] is not None
            else frozenset()
        )

    def _open(self, mode="r", buffering=-1):
        self._tracking()["open_calls"].append(mode)
        return super()._open(mode, buffering)


def test_native_checksum_protocol_is_optional_on_plain_mempath():
    # Base MemPath does not implement NativeChecksum -- no `.checksum`
    # attribute at all, confirming the protocol isn't mixed into the base
    # Path/Pathname ABC (per the plan's "optional per-subclass" contract).
    # NativeChecksum is a plain (not @runtime_checkable) typing.Protocol,
    # matching every other protocol in this package (BinaryOpen/Stat/Chmod
    # -- none use isinstance() checks either), so the check here is
    # duck-typed via hasattr(), not isinstance().
    plain = MemPath("/a.txt")
    assert not hasattr(plain, "checksum")


def test_dummy_path_native_checksum_preferred_over_streaming():
    path = _NativeChecksumMemPath("/a.txt")
    path.write_text("hello")
    path.force_checksum("deadbeef")
    path.open_calls.clear()  # drop the write_text() setup call above

    result = checksum_utils.native(path, "md5")

    assert result == "deadbeef"
    assert path.checksum_calls == ["md5"]
    # The whole point: no content was streamed through open("rb") to reach
    # this result.
    assert path.open_calls == []


def test_supported_checksums_default_is_empty_frozenset():
    # Base NativeChecksum.supported_checksums() (not overridden) returns an
    # empty frozenset -- the "no native support" advertisement, requiring
    # no NotImplementedError boilerplate just to say "none".
    path = _NativeChecksumMemPath("/a.txt")
    path.write_text("x")
    # No force_checksum() call -> _tracking()["forced"] is None -> the
    # dummy's own override reports frozenset() too, but exercise the
    # protocol's base implementation directly to prove IT specifically
    # returns frozenset() with zero setup.
    assert NativeChecksum.supported_checksums(path) == frozenset()


def test_supported_checksums_reflects_dummy_subclass_advertisement():
    path = _NativeChecksumMemPath("/a.txt")
    path.write_text("x")
    assert path.supported_checksums() == frozenset()

    path.force_checksum("deadbeef")
    assert path.supported_checksums() == frozenset({"md5"})


def test_native_helper_falls_back_to_none_on_notimplementederror():
    path = _NativeChecksumMemPath("/a.txt")
    path.write_text("hello")
    # _forced_checksum left None -> checksum() raises NotImplementedError.

    result = checksum_utils.native(path, "md5")

    assert result is None
    assert path.checksum_calls == ["md5"]


def test_native_helper_returns_none_for_path_without_protocol():
    plain = MemPath("/a.txt")
    plain.write_text("hello")
    assert checksum_utils.native(plain, "md5") is None


def test_shared_native_algorithm_prefers_md5_when_both_advertise_it():
    from pathlib_next.utils.sync import _shared_native_algorithm

    source = _NativeChecksumMemPath("/a.txt")
    source.write_text("x")
    source.force_checksum("h1")
    target = _NativeChecksumMemPath("/a.txt")
    target.write_text("x")
    target.force_checksum("h2")

    assert _shared_native_algorithm(source, target) == "md5"


def test_shared_native_algorithm_none_when_no_overlap():
    from pathlib_next.utils.sync import _shared_native_algorithm

    source = _NativeChecksumMemPath("/a.txt")
    source.write_text("x")
    source.force_checksum("h1")  # advertises {"md5"}
    target = MemPath("/a.txt")  # no supported_checksums() at all
    target.write_text("x")

    assert _shared_native_algorithm(source, target) is None


def test_shared_native_algorithm_none_when_either_side_lacks_advertisement():
    from pathlib_next.utils.sync import _shared_native_algorithm

    source = _NativeChecksumMemPath("/a.txt")
    source.write_text("x")
    # No force_checksum() -> supported_checksums() returns frozenset(),
    # same observable shape as "doesn't implement the protocol" -- both
    # collapse to None here, which is correct (the helper can't
    # distinguish, and `_default_checksums_match`'s fallback to trying
    # _DEFAULT_ALGORITHM directly handles it either way).
    target = _NativeChecksumMemPath("/a.txt")
    target.write_text("x")
    target.force_checksum("h2")

    assert _shared_native_algorithm(source, target) is None


def test_md5_sha256_stream_helpers_unaffected_by_native_protocol():
    # Direct callers of the original streaming functions (not going
    # through PathSyncer) must keep working unchanged.
    path = _NativeChecksumMemPath("/a.txt")
    path.write_text("hello")
    path.force_checksum("unused")

    import hashlib

    assert checksum_utils.md5(path) == hashlib.md5(b"hello").hexdigest()
    assert checksum_utils.sha256(path) == hashlib.sha256(b"hello").hexdigest()
    assert checksum_utils.stream(path, "md5") == hashlib.md5(b"hello").hexdigest()
    # Both streamed -- the native override was never consulted by these.
    assert path.checksum_calls == []


# --- PathSyncer integration -------------------------------------------


def test_pathsyncer_zero_open_calls_when_both_sides_native():
    source = _NativeChecksumMemPath("/")
    (source / "a.txt").write_text("same-content")
    target = _NativeChecksumMemPath("/")
    (target / "a.txt").write_text("same-content")

    src_file = source / "a.txt"
    tgt_file = target / "a.txt"
    src_file.force_checksum("samehash")
    tgt_file.force_checksum("samehash")
    # Clear the write_text() setup calls above -- only the sync() call
    # itself should be measured below.
    src_file.open_calls.clear()
    tgt_file.open_calls.clear()

    syncer = PathSyncer()
    events = []
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)

    from pathlib_next.utils.sync import SyncEvent

    assert SyncEvent.Copy not in events
    assert src_file.open_calls == []
    assert tgt_file.open_calls == []
    assert src_file.checksum_calls == ["md5"]
    assert tgt_file.checksum_calls == ["md5"]


def test_pathsyncer_native_mismatch_still_copies():
    source = _NativeChecksumMemPath("/")
    (source / "a.txt").write_text("source-content")
    target = _NativeChecksumMemPath("/")
    (target / "a.txt").write_text("target-content")

    (source / "a.txt").force_checksum("hash-a")
    (target / "a.txt").force_checksum("hash-b")

    syncer = PathSyncer()
    syncer.sync(source, target)

    assert (target / "a.txt").read_text() == "source-content"


def test_pathsyncer_mixed_capability_falls_back_to_streaming_both_sides():
    # source supports native checksums; target is a plain MemPath that
    # doesn't -- both sides must fall back to streaming rather than
    # comparing a native digest to nothing/a mismatched value.
    source = _NativeChecksumMemPath("/")
    (source / "a.txt").write_text("identical")
    (source / "a.txt").force_checksum("would-be-wrong-if-trusted-alone")

    target = MemPath("/")
    (target / "a.txt").write_text("identical")

    syncer = PathSyncer()
    events = []
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)

    from pathlib_next.utils.sync import SyncEvent

    # Content is identical -- streaming fallback on both sides correctly
    # detects "in sync", no Copy event, despite source's native digest
    # value being nonsense (proving it was NOT trusted alone).
    assert SyncEvent.Copy not in events
    # The native attempt was tried on source (and failed to find a partner
    # on target), so source's native path was exercised at least once.
    assert (source / "a.txt").checksum_calls == ["md5"]


def test_pathsyncer_mixed_capability_detects_real_difference():
    source = _NativeChecksumMemPath("/")
    (source / "a.txt").write_text("source-version")
    (source / "a.txt").force_checksum("irrelevant")

    target = MemPath("/")
    (target / "a.txt").write_text("target-version")

    syncer = PathSyncer()
    syncer.sync(source, target)

    assert (target / "a.txt").read_text() == "source-version"


def test_pathsyncer_custom_checksum_callable_bypasses_native_pairing():
    # A caller-supplied checksum callable must be invoked exactly as
    # before -- once per side, compared with `==` -- never routed through
    # the native-pairing policy.
    calls = []

    def custom(entry):
        calls.append(entry.path)
        return entry.stat.st_size

    source = _NativeChecksumMemPath("/")
    (source / "a.txt").write_text("aaa")
    (source / "a.txt").force_checksum("native-value-should-be-ignored")
    target = MemPath("/")
    # Target must already have the file for PathSyncer to reach the
    # checksum-comparison branch at all (an absent target file is always
    # just copied, no checksum() call either way). Different length than
    # source's "aaa" -- `custom` compares st_size, so same-length-different
    # -content wouldn't actually trigger a copy.
    (target / "a.txt").write_text("bbbbb")

    syncer = PathSyncer(custom)
    syncer.sync(source, target)

    assert (target / "a.txt").read_text() == "aaa"
    assert len(calls) >= 1
    # Custom callable path never calls the native checksum() method.
    assert (source / "a.txt").checksum_calls == []


# --- PathSyncer.quick_check (metadata-only pre-check for non-local pairs) --


class _NonLocalMemPath(MemPath):
    """`MemPath` with an explicit `is_local() -> False` and a controllable
    `st_mtime`, standing in for a real non-local `UriPath` (e.g. `SftpPath`)
    without needing a real/fake `Source`/DNS resolution -- `quick_check`
    only cares about the `is_local()` boolean and `PathAndStat.stat`'s
    `st_size`/`st_mtime`, both of which this fully controls. Also tracks
    `checksum_calls`/`open_calls` the same way `_NativeChecksumMemPath`
    does (see its docstring for why: PathSyncer re-derives its own path
    objects internally).
    """

    __slots__ = ()

    def is_local(self):
        return False

    def _tracking(self):
        store = self.backend.__dict__.setdefault("_quick_check_test_state", {})
        return store.setdefault(
            self.as_posix(),
            {"checksum_calls": [], "open_calls": [], "mtime": 0, "size": None},
        )

    def set_mtime(self, value):
        self._tracking()["mtime"] = value
        return self

    @property
    def checksum_calls(self):
        return self._tracking()["checksum_calls"]

    @property
    def open_calls(self):
        return self._tracking()["open_calls"]

    def stat(self, *, follow_symlinks=True):
        base = super().stat(follow_symlinks=follow_symlinks)
        if base.is_dir():
            return base
        return FileStat(
            is_dir=False, st_size=base.st_size, st_mtime=self._tracking()["mtime"]
        )

    def _open(self, mode="r", buffering=-1):
        self._tracking()["open_calls"].append(mode)
        return super()._open(mode, buffering)


def test_quick_check_skips_checksum_when_size_and_mtime_match():
    source = _NonLocalMemPath("/")
    (source / "a.txt").write_text("same-content")
    target = _NonLocalMemPath("/")
    (target / "a.txt").write_text("same-content")
    (source / "a.txt").set_mtime(1000)
    (target / "a.txt").set_mtime(1000)
    (source / "a.txt").open_calls.clear()
    (target / "a.txt").open_calls.clear()

    syncer = PathSyncer()  # quick_check=True by default
    events = []
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)

    from pathlib_next.utils.sync import SyncEvent

    assert SyncEvent.Copy not in events
    # Neither side's content was streamed -- size+mtime already agreed, so
    # the checksum step (native or streaming) was skipped entirely.
    assert (source / "a.txt").open_calls == []
    assert (target / "a.txt").open_calls == []


def test_quick_check_mismatched_mtime_falls_through_to_real_checksum():
    # Same size, different mtime -- quick_check must NOT conclude "changed"
    # on its own; it must fall through to a real checksum, which (content
    # is actually identical here) correctly finds them in sync.
    source = _NonLocalMemPath("/")
    (source / "a.txt").write_text("same-content")
    target = _NonLocalMemPath("/")
    (target / "a.txt").write_text("same-content")
    (source / "a.txt").set_mtime(1000)
    (target / "a.txt").set_mtime(2000)  # differs
    (source / "a.txt").open_calls.clear()
    (target / "a.txt").open_calls.clear()

    syncer = PathSyncer()
    events = []
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)

    from pathlib_next.utils.sync import SyncEvent

    # Content is identical -- the real checksum fallback correctly finds
    # "in sync" despite the mtime mismatch quick_check couldn't trust.
    assert SyncEvent.Copy not in events
    # Streaming was actually used this time (content had to be read to
    # decide) -- proves the mismatch genuinely fell through rather than
    # quick_check silently deciding anything.
    assert (source / "a.txt").open_calls == ["r"]
    assert (target / "a.txt").open_calls == ["r"]


def test_quick_check_mismatched_mtime_detects_real_difference():
    source = _NonLocalMemPath("/")
    (source / "a.txt").write_text("source-version")
    target = _NonLocalMemPath("/")
    (target / "a.txt").write_text("target-version")
    (source / "a.txt").set_mtime(1000)
    (target / "a.txt").set_mtime(2000)

    syncer = PathSyncer()
    syncer.sync(source, target)

    assert (target / "a.txt").read_text() == "source-version"


def test_quick_check_false_disables_pre_check():
    source = _NonLocalMemPath("/")
    (source / "a.txt").write_text("same-content")
    target = _NonLocalMemPath("/")
    (target / "a.txt").write_text("same-content")
    (source / "a.txt").set_mtime(1000)
    (target / "a.txt").set_mtime(1000)
    (source / "a.txt").open_calls.clear()
    (target / "a.txt").open_calls.clear()

    syncer = PathSyncer(quick_check=False)
    events = []
    syncer._hook = lambda s, t, e, dry: events.append(e)
    syncer.sync(source, target)

    from pathlib_next.utils.sync import SyncEvent

    # Still correctly in sync (content matches) -- but reached via the real
    # checksum path this time, proving quick_check=False actually disabled
    # the metadata-only shortcut even though size+mtime both matched.
    assert SyncEvent.Copy not in events
    assert (source / "a.txt").open_calls == ["r"]
    assert (target / "a.txt").open_calls == ["r"]


def test_quick_check_does_not_apply_to_local_to_local_pairs(tmp_path):
    # LocalPath has no is_local() method -- _is_local()'s "no method"
    # default treats it as local, so quick_check must never engage for a
    # local-to-local pair regardless of the constructor default. Proven by
    # a checksum spy: even with identical size/mtime metadata (which WOULD
    # trigger quick_check's skip for a non-local pair), the checksum
    # callable must still be invoked for a local pair.
    import os
    import time

    import pathlib_next

    calls = []

    def spy_checksum(entry):
        calls.append(entry.path)
        return "same-value-always"  # forces "in sync" so no copy happens

    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    (src_dir / "a.txt").write_text("same-content")
    (dst_dir / "a.txt").write_text("same-content")
    same_time = time.time()
    os.utime(src_dir / "a.txt", (same_time, same_time))
    os.utime(dst_dir / "a.txt", (same_time, same_time))

    syncer = PathSyncer(spy_checksum)  # quick_check=True (default)
    syncer.sync(pathlib_next.LocalPath(src_dir), pathlib_next.LocalPath(dst_dir))

    # The checksum callable WAS invoked despite matching size+mtime --
    # proves quick_check's skip-the-checksum-call behavior never engaged
    # for this local-to-local pair.
    assert len(calls) >= 1
