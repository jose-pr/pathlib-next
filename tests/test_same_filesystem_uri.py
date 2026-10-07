"""`UriPath._same_filesystem()`: a backend the caller supplied names a
filesystem; one a path derived for itself says no more than the URI does.

Before this rule two separately built paths to one URL each derived their
own backend -- `samefile()` itself triggers it, through `stat()` -- and the
"same file" guard never fired between them."""

import io

import pytest

from pathlib_next.path import _contains, _same_file
from pathlib_next.uri import UriPath, _DerivedBackend
from pathlib_next.utils.stat import FileStat
from pathlib_next.utils.sync import PathSyncer, _paths_overlap

#: An IP literal (TEST-NET-1): `PathSyncer` asks whether a URI is local, and
#: a host NAME would go to DNS, which the suite blocks.
URL = "samefs-probe://192.0.2.1/dir/f.txt"


class Store(dict):
    """One server. Unhashable and equal to every other empty one, like a
    real dict-shaped backend: only identity can tell two apart."""


class SlottedStore:
    """A backend that cannot be weakly referenced."""

    __slots__ = ("files",)

    def __init__(self):
        self.files = {}

    def __contains__(self, key):
        return key in self.files

    def __getitem__(self, key):
        return self.files[key]

    def __setitem__(self, key, value):
        self.files[key] = value

    def pop(self, key):
        return self.files.pop(key)

    def __iter__(self):
        return iter(self.files)


#: What a derived backend talks to: the server the URL names.
SERVER = Store()


class _Writer(io.BytesIO):
    def __init__(self, store, key):
        super().__init__()
        self._store, self._key = store, key
        store[key] = b""  # truncates at open, as a real server does

    def close(self):
        if not self.closed:
            self._store[self._key] = self.getvalue()
        super().close()


class ProbePath(UriPath):
    __SCHEMES = ("samefs-probe",)
    __slots__ = ()

    def _initbackend(self):
        return _Derived(SERVER)

    @property
    def _files(self):
        backend = self.backend
        return backend.server if isinstance(backend, _Derived) else backend

    def stat(self, *, follow_symlinks=True):
        files, key = self._files, self.path.rstrip("/")
        if key in files:
            return FileStat(st_mode=0o100644, st_size=len(files[key]))
        if any(name.startswith(key + "/") for name in files):
            return FileStat(st_mode=0o040755)
        raise FileNotFoundError(key)

    def _listdir(self):
        prefix = self.path.rstrip("/") + "/"
        return sorted(
            {
                n[len(prefix) :].split("/")[0]
                for n in self._files
                if n.startswith(prefix)
            }
        )

    def _mkdir(self, mode):
        pass

    def unlink(self, missing_ok=False):
        self._files.pop(self.path)

    def _open(self, mode="r", buffering=-1):
        if "w" in mode:
            return _Writer(self._files, self.path)
        return io.BytesIO(self._files[self.path])


class _Derived:
    """A per-path session onto `SERVER`: every path that derives one gets
    its own object, which is what made two paths look unrelated."""

    def __init__(self, server):
        self.server = server


@pytest.fixture(autouse=True)
def _server():
    SERVER.clear()
    SERVER["/dir/f.txt"] = b"DATA"
    yield
    SERVER.clear()


def _guards(a, b):
    return _same_file(a, b), _contains(a.parent, b), _paths_overlap(a.parent, b.parent)


def test_dispatch():
    assert type(UriPath(URL)) is ProbePath


@pytest.mark.parametrize("touch", ["neither", "source", "both"])
def test_separately_built_paths_to_one_url_are_the_same_file(touch):
    a, b = UriPath(URL), UriPath(URL)
    if touch in ("source", "both"):
        a.stat()
    if touch == "both":
        b.stat()
        assert a.backend is not b.backend  # each derived its own

    assert _guards(a, b) == (True, True, True)
    with pytest.raises(OSError, match="same file"):
        a.copy(b, overwrite=True)
    assert SERVER["/dir/f.txt"] == b"DATA"


def test_sync_of_one_url_onto_itself_is_refused():
    a, b = UriPath(URL).parent, UriPath(URL).parent
    a.stat(), b.stat()
    with pytest.raises(ValueError, match="overlap"):
        PathSyncer(lambda p: "x", remove_missing=True).sync(a, b)
    assert SERVER["/dir/f.txt"] == b"DATA"


def test_a_supplied_backend_against_a_fresh_path_is_the_same_file():
    # The fresh side will connect to the authority both URLs name; nothing
    # says the supplied backend goes anywhere else.
    a, b = UriPath(URL, backend=SERVER), UriPath(URL)
    assert _guards(a, b) == (True, True, True)
    assert _guards(b, a) == (True, True, True)
    with pytest.raises(OSError, match="same file"):
        a.copy(b, overwrite=True)
    assert SERVER["/dir/f.txt"] == b"DATA"


def test_two_supplied_backends_are_two_hosts():
    one, two = Store({"/dir/f.txt": b"ONE"}), Store({"/dir/f.txt": b"TWO"})
    a, b = UriPath(URL, backend=one), UriPath(URL, backend=two)
    assert a == b
    assert _guards(a, b) == (False, False, False)

    a.copy(b, overwrite=True)
    assert two["/dir/f.txt"] == b"ONE"
    assert one["/dir/f.txt"] == b"ONE"

    one["/dir/g.txt"] = b"G"
    PathSyncer(lambda entry: entry.stat.st_size).sync(a.parent, b.parent)
    assert two["/dir/g.txt"] == b"G"
    assert one["/dir/g.txt"] == b"G"


def test_a_shared_supplied_backend_is_one_host():
    store = Store({"/dir/f.txt": b"ONE"})
    a, b = UriPath(URL, backend=store), UriPath(URL, backend=store)
    assert _guards(a, b) == (True, True, True)
    assert _guards(a, a.parent / "f.txt") == (True, True, True)
    with pytest.raises(OSError, match="same file"):
        a.copy(b, overwrite=True)
    assert store["/dir/f.txt"] == b"ONE"


def test_a_derived_backend_that_cannot_be_weakly_referenced(monkeypatch):
    # The path records that it built the backend, so an object that cannot be
    # tracked by identity is still derived: two paths to one URL are the same
    # file and a copy onto itself is refused.
    monkeypatch.setattr(ProbePath, "_initbackend", lambda self: SlottedStore())
    a, b = UriPath(URL), UriPath(URL)
    a.backend["/dir/f.txt"] = b"A"
    b.backend["/dir/f.txt"] = b"B"
    assert a._supplied_backend() is None
    assert a._same_filesystem(b)
    assert a._same_filesystem(a.parent / "f.txt")
    with pytest.raises(OSError, match="same file"):
        a.copy(b, overwrite=True)
    assert a.backend["/dir/f.txt"] == b"A"
    assert b.backend["/dir/f.txt"] == b"B"


def test_a_supplied_backend_that_cannot_be_weakly_referenced_stays_supplied():
    one, two = SlottedStore(), SlottedStore()
    a, b = UriPath(URL, backend=one), UriPath(URL, backend=two)
    assert a._supplied_backend() is one
    assert not a._same_filesystem(b)
    assert (a / "x")._supplied_backend() is one


def test_supplied_backend_is_never_built_by_asking():
    a = UriPath(URL)
    assert a._supplied_backend() is None
    assert a._same_filesystem(UriPath(URL))
    assert a._backend is None


def test_an_unreferenceable_backend_class_can_mark_itself_derived(monkeypatch):
    # A tuple cannot be weakly referenced (`HttpBackend` is one), so its
    # class says so instead.
    class Marked(tuple, _DerivedBackend):
        __slots__ = ()

    monkeypatch.setattr(ProbePath, "_initbackend", lambda self: Marked())
    a, b = UriPath(URL), UriPath(URL)
    assert a.backend is not b.backend
    assert a._supplied_backend() is None
    assert a._same_filesystem(b)
