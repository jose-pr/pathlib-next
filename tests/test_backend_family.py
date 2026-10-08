"""Paths derived from one another on one endpoint share the backend the first
of them to do I/O derives, and deriving builds nothing. Nothing else shares:
not another endpoint, not a supplied backend, not a path built separately."""

import copy
import http.server
import pickle
import threading
import time

import pytest

from pathlib_next.uri import Uri, UriPath


class _Backend:
    def __init__(self, tag):
        self.tag = tag


class FamilyScopePath(UriPath):
    """A scheme whose backend says which endpoint built it and records every
    build."""

    __SCHEMES = ("family-scope",)
    __slots__ = ()
    built = []
    delay = 0.0

    def _initbackend(self):
        if self.delay:
            time.sleep(self.delay)
        self.built.append(self.source.host)
        return _Backend(f"derived-{self.source.host}")


@pytest.fixture(autouse=True)
def _forget_built():
    FamilyScopePath.built.clear()
    FamilyScopePath.delay = 0.0
    yield
    FamilyScopePath.delay = 0.0


def _family_of(p):
    return [
        p.parent,
        p.parent.parent,
        *p.parents,
        p.with_name("z"),
        p.with_suffix(".t"),
        p.with_stem("s"),
        p.with_query("q=1"),
        p.with_fragment("f"),
        p.with_path("/other"),
        p / "k",
        p / Uri("k"),
        p / "a" / "b",
        p.joinpath("k", Uri("m")),
        UriPath(p, "k"),
        FamilyScopePath(p, "k"),
        p._make_child_relpath("k"),
        p._coerce_target("k"),
        p._coerce_target("family-scope://a/e"),
        p._rename_target("k"),
        p.with_source(p.source),
        copy.copy(p),
        copy.deepcopy(p),
    ]


def test_deriving_builds_nothing_and_every_relative_shares_one_backend():
    p = UriPath("family-scope://a/x/y.txt")
    relatives = _family_of(p)
    assert FamilyScopePath.built == []
    assert all(d._backend is None for d in relatives) and p._backend is None
    backends = {id(d.backend) for d in relatives}
    assert len(backends) == 1
    assert p.backend is relatives[0].backend
    assert FamilyScopePath.built == ["a"]


def test_the_first_relative_to_do_io_fills_the_slot_for_the_rest():
    p = UriPath("family-scope://a/x/y.txt")
    relatives = _family_of(p)
    middle = relatives[len(relatives) // 2]
    built = middle.backend
    assert FamilyScopePath.built == ["a"]
    assert p.backend is built
    assert all(d.backend is built for d in relatives)
    assert FamilyScopePath.built == ["a"]


def test_a_relative_made_after_the_slot_is_filled_reads_it():
    p = UriPath("family-scope://a/x")
    first = p / "one"
    built = first.backend
    for later in (p / "two", p.parent, first.parent / "three", copy.copy(p)):
        assert later.backend is built
    assert FamilyScopePath.built == ["a"]


def test_twenty_children_of_one_fresh_root_build_one_backend():
    root = UriPath("family-scope://a/")
    children = [root / f"f{i}.txt" for i in range(20)]
    assert FamilyScopePath.built == []
    assert len({id(c.backend) for c in children}) == 1
    assert FamilyScopePath.built == ["a"]


def test_a_walk_down_a_tree_shares_one_backend():
    root = UriPath("family-scope://a/")
    leaves = [root / f"d{i}" / "sub" / "leaf" for i in range(5)]
    assert len({id(leaf.backend) for leaf in leaves}) == 1
    assert root.backend is leaves[0].backend
    assert FamilyScopePath.built == ["a"]


def test_paths_built_separately_do_not_share():
    one = UriPath("family-scope://a/x")
    two = UriPath("family-scope://a/x")
    assert one.backend is not two.backend
    assert (one / "k").backend is one.backend
    assert (two / "k").backend is two.backend
    assert FamilyScopePath.built == ["a", "a"]


def test_an_equal_path_built_from_a_string_is_not_in_the_family():
    root = UriPath("family-scope://a/")
    a = root / "x"
    b = UriPath("family-scope://a/x")
    assert a.backend is not b.backend


# --- another endpoint never shares ---


def _derived_tag(path):
    return path.backend.tag


def test_a_path_that_crosses_to_another_endpoint_builds_its_own():
    p = UriPath("family-scope://u@a:1/d")
    crossing = [
        p.with_source(UriPath("family-scope://b/").source),
        p / Uri("family-scope://b/e"),
        UriPath(p, Uri("family-scope://b/e")),
        p._coerce_target("family-scope://b/e"),
        p / Uri("family-scope://u@a:2/e"),
        p / Uri("family-scope://other@a:1/e"),
        p / Uri("family-scope://u@c:1/e"),
    ]
    assert FamilyScopePath.built == []
    for path in crossing:
        assert path._derived_cell is None or path._derived_cell is not p._derived_cell
    tags = [_derived_tag(path) for path in crossing]
    assert tags == [
        "derived-b",
        "derived-b",
        "derived-b",
        "derived-b",
        "derived-a",
        "derived-a",
        "derived-c",
    ]
    assert p.backend.tag == "derived-a"
    # One build for the source endpoint, one for each crossing path's own.
    assert len(FamilyScopePath.built) == 1 + len(crossing)


def test_a_userinfo_or_port_difference_does_not_join_the_family():
    base = UriPath("family-scope://user@a:1/d")
    kept = base / "x"
    same = base / Uri("family-scope://USER@a:1/e")
    other = base / Uri("family-scope://user@a:2/e")
    assert kept.backend is base.backend
    assert other.backend is not base.backend
    # The host compares without case, the userinfo with it.
    assert (base / Uri("family-scope://user@A:1/e")).backend is base.backend
    assert same.backend is not base.backend


def test_the_sourceless_result_of_relative_to_carries_no_slot():
    root = UriPath("family-scope://a/root")
    rel = (root / "x" / "y").relative_to(root)
    assert rel._derived_cell is None and rel._backend is None
    assert (rel / "z")._derived_cell is None
    other = UriPath("family-scope://b/root")
    joined = other / rel
    assert joined.backend is other.backend
    assert FamilyScopePath.built == ["b"]
    assert rel._derived_cell is None


# --- a supplied backend is untouched ---


def test_a_supplied_backend_is_carried_as_before_and_never_replaced():
    p = UriPath("family-scope://a/x")
    relatives = [p / "one", p.parent]
    supplied = _Backend("supplied")
    mine = p.with_backend(supplied)
    assert mine.backend is supplied
    assert (mine / "k").backend is supplied
    assert mine.parent.backend is supplied
    assert mine.with_name("z")._supplied_backend() is supplied
    assert FamilyScopePath.built == []
    # The unsupplied relatives still derive their own, shared among them.
    assert relatives[0].backend is relatives[1].backend is p.backend
    assert p.backend is not supplied
    assert FamilyScopePath.built == ["a"]
    # A supplied path's relatives are not drawn into the family either.
    assert (mine / "k").backend is supplied


def test_a_supplied_backend_wins_over_a_fresh_path_on_the_same_endpoint():
    supplied = _Backend("supplied")
    root = UriPath("family-scope://a/d", backend=supplied)
    fresh = UriPath("family-scope://a/e")
    assert UriPath(root, fresh).backend is supplied
    assert UriPath(fresh, root).backend is supplied
    assert (root / fresh).backend is supplied
    assert FamilyScopePath.built == []


def test_supplied_and_derived_keep_their_meaning_for_same_filesystem_and_scope():
    p = UriPath("family-scope://a/x")
    q = p / "k"
    assert p._supplied_backend() is None and q._supplied_backend() is None
    assert p.backend is q.backend
    assert p._supplied_backend() is None and q._supplied_backend() is None
    assert p._same_filesystem(q)
    other = UriPath("family-scope://a/x", backend=_Backend("s"))
    assert p._same_filesystem(other)
    assert not other._same_filesystem(
        UriPath("family-scope://a/y", backend=_Backend("t"))
    )


def test_a_pickle_or_copy_of_a_family_member_follows_the_rules_of_a_path():
    p = UriPath("family-scope://a/x")
    q = p / "k"
    restored = pickle.loads(pickle.dumps(q))
    assert restored == q
    assert restored._derived_cell is None
    assert restored.backend is not q.backend
    assert FamilyScopePath.built == ["a", "a"]
    duplicate = copy.copy(q)
    assert duplicate.backend is q.backend


# --- threads ---


def test_two_threads_doing_first_io_on_two_relatives_end_with_one_backend():
    FamilyScopePath.delay = 0.05
    for _ in range(20):
        FamilyScopePath.built.clear()
        root = UriPath("family-scope://a/")
        pair = (root / "one", root / "two")
        start = threading.Barrier(2)
        seen = []

        def use(path):
            start.wait()
            seen.append(path.backend)

        threads = [threading.Thread(target=use, args=(path,)) for path in pair]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert len(seen) == 2 and seen[0] is seen[1]
        assert FamilyScopePath.built == ["a"]
        assert root.backend is seen[0]


# --- the effect on a connection ---


class _Counting(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.server.accepted.append(self.client_address)
        super().setup()

    def log_message(self, format, *args):
        pass

    def _serve(self):
        self.send_response(200)
        self.send_header("Content-Length", "3")
        self.end_headers()

    do_HEAD = do_GET = _serve


@pytest.fixture
def counting_server():
    requests = pytest.importorskip("requests")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Counting)
    server.accepted = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_twenty_children_of_one_fresh_root_use_one_connection(counting_server):
    root = UriPath(counting_server.base + "/")
    sizes = [(root / f"f{i}.txt").stat().st_size for i in range(20)]
    assert sizes == [3] * 20
    assert len(counting_server.accepted) == 1


def test_a_parent_and_the_path_use_one_connection(counting_server):
    p = UriPath(counting_server.base + "/d/f.txt")
    assert p.parent.exists()
    assert p.exists()
    assert len(counting_server.accepted) == 1


def test_paths_built_separately_still_use_one_connection_each(counting_server):
    for i in range(3):
        UriPath(f"{counting_server.base}/f{i}.txt").stat()
    assert len(counting_server.accepted) == 3
