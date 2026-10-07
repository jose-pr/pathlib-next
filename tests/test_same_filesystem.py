"""`Path._same_filesystem()`: how the copy/move/sync guards learn that two
equally spelled paths live in different namespaces.

`FrontPath` is shaped like a third-party path type that fronts a delegate
under an attribute name of its own -- deliberately NOT `_backend`, the
private name the guards used to sniff, which made two hosts holding
`/app.conf` "the same file"."""

import pytest

from pathlib_next import Path
from pathlib_next.mempath import MemPath
from pathlib_next.path import _contains, _same_file
from pathlib_next.utils.sync import PathSyncer, _paths_overlap


class FrontPath(Path):
    """A host-style path: every operation goes to a `MemPath` on the host's
    own store. Says nothing about which store that is."""

    __slots__ = ("_delegate",)

    def __init__(self, *segments, delegate: MemPath = None):
        # `path / "x"` builds `type(self)(self, "x")`: the store is taken
        # from the first path among the segments, as `MemPath` does.
        if delegate is None:
            delegate = MemPath(
                *(s._delegate if isinstance(s, FrontPath) else s for s in segments)
            )
        self._delegate = delegate

    @property
    def segments(self):
        return self._delegate.segments

    @property
    def parts(self):
        return tuple(self._delegate.segments)

    @property
    def parent(self):
        return type(self)(delegate=self._delegate.parent)

    def with_segments(self, *segments):
        return type(self)(delegate=self._delegate.with_segments(*segments))

    def relative_to(self, other):
        raise NotImplementedError()

    def as_uri(self):
        return "front:" + self.as_posix()

    def stat(self, *, follow_symlinks=True):
        return self._delegate.stat(follow_symlinks=follow_symlinks)

    def iterdir(self):
        for child in self._delegate.iterdir():
            yield type(self)(delegate=child)

    def _open(self, mode="r", buffering=-1):
        return self._delegate._open(mode, buffering)

    def _mkdir(self, mode):
        return self._delegate._mkdir(mode)

    def unlink(self, missing_ok=False):
        return self._delegate.unlink(missing_ok=missing_ok)

    def rmdir(self):
        return self._delegate.rmdir()


class HostPath(FrontPath):
    """The same type once it answers the hook."""

    __slots__ = ()

    def _same_filesystem(self, other):
        return self._delegate.backend is other._delegate.backend


def _host(cls):
    return cls("/")


def _two_hosts(cls):
    source, target = _host(cls), _host(cls)
    (source / "app.conf").write_bytes(b"payload")
    (target / "app.conf").write_bytes(b"old")
    return source, target


def _size(entry):
    return entry.stat.st_size


# --- a type that answers the hook ------------------------------------------


def test_copy_between_two_hosts_with_the_same_spelling():
    source, target = _two_hosts(HostPath)
    assert source / "app.conf" == target / "app.conf"  # equality is blind

    (source / "app.conf").copy(target / "app.conf", overwrite=True)

    assert (target / "app.conf").read_bytes() == b"payload"
    assert (source / "app.conf").read_bytes() == b"payload"


def test_move_between_two_hosts_with_the_same_spelling():
    source, target = _two_hosts(HostPath)

    (source / "app.conf").move(target / "app.conf", overwrite=True)

    assert (target / "app.conf").read_bytes() == b"payload"
    assert not (source / "app.conf").exists()


def test_recursive_copy_between_two_hosts_with_the_same_spelling():
    source, target = _host(HostPath), _host(HostPath)
    (source / "etc").mkdir()
    (source / "etc" / "app.conf").write_bytes(b"payload")

    (source / "etc").copy(target / "etc", recursive=True)

    assert (target / "etc" / "app.conf").read_bytes() == b"payload"
    assert (source / "etc" / "app.conf").read_bytes() == b"payload"


def test_sync_between_two_hosts_with_the_same_spelling():
    source, target = _host(HostPath), _host(HostPath)
    (source / "etc").mkdir()
    (source / "etc" / "app.conf").write_bytes(b"payload")
    (target / "etc").mkdir()

    PathSyncer(_size).sync(source / "etc", target / "etc")

    assert (target / "etc" / "app.conf").read_bytes() == b"payload"
    assert (source / "etc" / "app.conf").read_bytes() == b"payload"


def test_one_host_is_still_guarded():
    host = _host(HostPath)
    (host / "etc").mkdir()
    (host / "etc" / "app.conf").write_bytes(b"payload")
    again = HostPath(delegate=MemPath("/etc/app.conf", backend=host._delegate.backend))

    with pytest.raises(OSError, match="same file"):
        (host / "etc" / "app.conf").copy(again, overwrite=True)
    assert (host / "etc" / "app.conf").read_bytes() == b"payload"

    with pytest.raises(OSError, match="into itself"):
        (host / "etc").copy(host / "etc" / "backup", recursive=True)
    assert not (host / "etc" / "backup").exists()

    with pytest.raises(ValueError, match="overlap"):
        PathSyncer(_size, remove_missing=True).sync(host / "etc", again.parent)
    assert (host / "etc" / "app.conf").read_bytes() == b"payload"


def test_the_three_guards_agree():
    source, target = _two_hosts(HostPath)
    a, b = source / "app.conf", target / "app.conf"
    assert not _same_file(a, b)
    assert not _contains(a.parent, b)
    assert not _paths_overlap(a.parent, b.parent)

    same = a.parent / "app.conf"
    assert _same_file(a, same)
    assert _contains(a.parent, same)
    assert _paths_overlap(a.parent, same.parent)


# --- a type that says nothing ----------------------------------------------


def test_a_type_that_does_not_answer_is_judged_by_its_spelling():
    # The default cannot tell two stores apart, and refusing is the answer
    # that loses no data: a single-store type gets its self-copy guard for
    # free, and a multi-store type overrides one method.
    source, target = _two_hosts(FrontPath)

    with pytest.raises(OSError, match="same file"):
        (source / "app.conf").copy(target / "app.conf", overwrite=True)

    assert (source / "app.conf").read_bytes() == b"payload"
    assert (target / "app.conf").read_bytes() == b"old"


def test_default_keeps_a_single_store_type_from_copying_onto_itself():
    host = _host(FrontPath)
    (host / "app.conf").write_bytes(b"payload")

    with pytest.raises(OSError, match="same file"):
        (host / "app.conf").copy(host / "app.conf", overwrite=True)

    assert (host / "app.conf").read_bytes() == b"payload"


# --- MemPath answers for itself ---------------------------------------------


def test_mempath_answers_by_backend_identity():
    a, b = MemPath("/x"), MemPath("/x")
    assert a == b
    assert not a._same_filesystem(b)
    assert not b._same_filesystem(a)
    assert a._same_filesystem(a / "child")
    assert a._same_filesystem(MemPath("/y", backend=a.backend))


# --- a type that spells one node several ways ---------------------------------


class AliasPath(HostPath):
    """A host-style path whose `name` and `NAME` are one node."""

    __slots__ = ()

    def _node_key(self):
        names = tuple(s.lower() for s in self._delegate.segments if s)
        return self._delegate.backend, names


class OtherAliasPath(AliasPath):
    """Another class over the same stores."""

    __slots__ = ()


def test_node_key_makes_two_spellings_of_one_node_the_same_file():
    host = _host(AliasPath)
    (host / "etc").mkdir()
    (host / "etc" / "app.conf").write_bytes(b"payload")
    shout = AliasPath(delegate=MemPath("/ETC/APP.CONF", backend=host._delegate.backend))

    assert (host / "etc" / "app.conf") != shout
    assert _same_file(host / "etc" / "app.conf", shout)
    assert _contains(host / "etc", shout)
    assert not _same_file(host / "etc", shout)
    with pytest.raises(OSError, match="same file"):
        (host / "etc" / "app.conf").copy(shout, overwrite=True)
    assert (host / "etc" / "app.conf").read_bytes() == b"payload"


def test_node_key_is_compared_across_classes():
    host = _host(AliasPath)
    (host / "a.txt").write_bytes(b"payload")
    other = OtherAliasPath(delegate=MemPath("/A.TXT", backend=host._delegate.backend))

    assert _same_file(host / "a.txt", other)
    assert not _same_file(host / "a.txt", _host(OtherAliasPath) / "a.txt")


def test_node_key_of_a_nested_target_is_inside_its_source():
    root = MemPath("/")
    (root / "d").mkdir()
    inner = MemPath("d/x", backend=root.backend)
    assert _contains(root / "d", inner)
    assert not _contains(inner, root / "d")
    assert _contains(root, inner)
