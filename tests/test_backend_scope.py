"""A backend (session, credentials) belongs to one endpoint: scheme,
userinfo, host and port. A derived path takes the one its source path holds,
only when it is on that same endpoint, and never builds one.

The HTTP tests use two loopback servers and assert the headers the SECOND
one received; the others record which backend each path ended up holding.
"""

import http.server
import io
import os
import pathlib
import subprocess
import sys
import threading

import pytest

from pathlib_next.path import _same_file
from pathlib_next.uri import Uri, UriPath

requests = pytest.importorskip("requests")

from pathlib_next.uri.schemes.http import HttpPath


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answers every GET/HEAD with a body and records each request's path
    and the two headers the tests plant on a session."""

    def _reply(self):
        self.server.log.append(
            (
                self.command,
                self.path,
                self.headers.get("Authorization"),
                self.headers.get("X-Session"),
            )
        )
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_HEAD = _reply

    def log_message(self, *args):
        pass


@pytest.fixture
def two_servers():
    """`(a, b)`: base URLs of two loopback servers, each with a `.log` of
    `(method, path, Authorization, X-Session)` rows."""
    servers = []
    for _ in range(2):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        server.log = []
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        servers.append(server)
    try:
        yield tuple(
            type(
                "Server",
                (),
                {"url": f"http://127.0.0.1:{s.server_port}", "log": s.log},
            )
            for s in servers
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def _session(**headers):
    session = requests.Session()
    session.trust_env = False
    session.headers.update(headers)
    return session


def _read(path):
    assert path.read_bytes() == b"hello"


# --- a backend never follows a path to another endpoint --------------------


def _crossings():
    """Ways to reach the other server from `base`. `other` is a plain `Uri`
    for it; a route may also spell it as a `str` or a `UriPath`."""
    return {
        "base / Uri": lambda base, other: base / other,
        "base / UriPath": lambda base, other: base / UriPath(other.as_uri()),
        "parent / str": lambda base, other: (base / other).parent / "f",
        "(base / Uri) / str": lambda base, other: (base / other) / "x",
        "(base / UriPath) / str": lambda base, other: (
            (base / UriPath(other.as_uri())) / "x"
        ),
        "(base / Uri) / str with a trailing slash": lambda base, other: (
            (base / other) / "x/"
        ),
        "(base / Uri) / absolute str": lambda base, other: (base / other) / "/abs",
        "joinpath(Uri, str)": lambda base, other: base.joinpath(other, "x"),
        "UriPath(base, str) / str": lambda base, other: (
            UriPath(base, other.as_uri()) / "x"
        ),
        "HttpPath(base, str) / str": lambda base, other: (
            HttpPath(base, other.as_uri()) / "x"
        ),
        "UriPath(base, Uri) / str": lambda base, other: UriPath(base, other) / "x",
        "with_name": lambda base, other: ((base / other) / "f").with_name("z"),
        "with_suffix": lambda base, other: ((base / other) / "f").with_suffix(".t"),
        "_make_child_relpath": lambda base, other: (
            (base / other)._make_child_relpath("x")
        ),
        "with_source": lambda base, other: base.with_source(other.source),
        "copy destination": lambda base, other: base._coerce_target(other.as_uri()),
        "copy destination after a crossing": lambda base, other: (
            (base / other)._coerce_target(other.as_uri().rstrip("/") + "/z")
        ),
    }


@pytest.mark.parametrize("route", list(_crossings()))
def test_a_session_never_reaches_another_server(two_servers, route):
    a, b = two_servers
    base = UriPath(f"{a.url}/api/d/f.txt").with_session(
        _session(Authorization="Bearer SECRET-FOR-A")
    )
    other = Uri(f"{b.url}/other/d/")

    crossed = _crossings()[route](base, other)
    assert crossed.source.port == int(b.url.rsplit(":", 1)[1])
    crossed.read_bytes()

    assert b.log, "the request must have gone to the second server"
    assert [row[2] for row in b.log] == [None] * len(b.log)
    assert a.log == []


def test_a_session_still_follows_a_join_on_its_own_server(two_servers):
    a, _ = two_servers
    base = UriPath(f"{a.url}/api/").with_session(_session(**{"X-Session": "mine"}))
    for path in (
        base / "x",
        base / "sub" / "x",
        base.joinpath("sub", "x"),
        base / Uri("x"),
        UriPath(base, "x"),
        (base / "sub/x").parent / "y",
        (base / "x").with_name("y"),
        base._coerce_target("y"),
        base._coerce_target(f"{a.url}/api/y"),
    ):
        a.log.clear()
        _read(path)
        assert {row[3] for row in a.log} == {"mine"}, path


def test_the_mirroring_idiom_keeps_the_destination_session(two_servers):
    """`dst_root / src.relative_to(src_root)` is the way to spell the same
    file on another endpoint: the destination's own session must be used,
    not an ambient default and not the source's."""
    a, b = two_servers
    src_root = UriPath(f"{a.url}/api/").with_session(
        _session(Authorization="Bearer SECRET-FOR-A")
    )
    dst_root = UriPath(f"{b.url}/mirror/").with_session(
        _session(**{"X-Session": "dst"})
    )
    src = src_root / "sub" / "f.txt"
    rel = src.relative_to(src_root)
    assert rel._backend is None

    routes = {
        "dst_root / rel": dst_root / rel,
        "dst_root.joinpath(rel)": dst_root.joinpath(rel),
        "HttpPath(dst_root, rel)": HttpPath(dst_root, rel),
        "(dst_root / rel).with_name": (dst_root / rel).with_name("g"),
        "(dst_root / rel.parent) / name": (dst_root / src.parent.relative_to(src_root))
        / "f.txt",
        "dst_root / rel.path": dst_root / rel.path,
    }
    for label, path in routes.items():
        b.log.clear()
        _read(path)
        assert {row[2:] for row in b.log} == {(None, "dst")}, label
    assert a.log == []


# --- scheme-neutral: which backend a derived path holds --------------------


class _Backend:
    def __init__(self, tag):
        self.tag = tag


class TaggedScopePath(UriPath):
    """A scheme whose backend says which endpoint built it."""

    __SCHEMES = ("tagged-scope",)
    __slots__ = ()
    built = []

    def _initbackend(self):
        self.built.append(self.source.host)
        return _Backend(f"derived-{self.source.host}")


@pytest.fixture(autouse=True)
def _forget_built():
    TaggedScopePath.built.clear()


def _tag(path):
    return path.backend.tag


def test_the_backend_of_the_rightmost_segment_on_the_result_endpoint_wins():
    a = UriPath("tagged-scope://a/d", backend=_Backend("supplied-a"))
    b = UriPath("tagged-scope://b/d", backend=_Backend("supplied-b"))
    assert _tag(UriPath(a, b)) == "supplied-b"
    assert _tag(UriPath(b, a)) == "supplied-a"
    assert _tag(UriPath(a, b, "x")) == "supplied-b"
    # A later segment of another endpoint does not displace the one whose
    # endpoint the result has.
    assert _tag(UriPath(b, "tagged-scope://b/e", Uri("x"))) == "supplied-b"
    assert _tag(UriPath(a, Uri("tagged-scope://c/e"))) == "derived-c"


def test_a_sourceless_relative_path_carries_no_backend():
    root = UriPath("tagged-scope://a/root", backend=_Backend("supplied-a"))
    rel = (root / "x" / "y").relative_to(root)
    assert rel._backend is None
    assert TaggedScopePath.built == []
    other = UriPath("tagged-scope://b/root", backend=_Backend("supplied-b"))
    assert _tag(other / rel) == "supplied-b"
    assert _tag((other / rel) / "z") == "supplied-b"
    assert _tag(UriPath(other, rel)) == "supplied-b"


def test_a_path_built_with_an_explicit_backend_keeps_it_even_without_a_source():
    backend = _Backend("explicit")
    bare = TaggedScopePath("relative/name", backend=backend)
    assert not bare.source
    assert bare.backend is backend
    assert bare.parent.backend is backend
    assert (bare / "x").backend is backend
    assert bare.with_name("z")._supplied_backend() is backend


def test_a_userinfo_or_port_difference_is_another_endpoint():
    base = UriPath("tagged-scope://user@a:1/d", backend=_Backend("supplied"))
    for other in (
        "tagged-scope://a:1/d",
        "tagged-scope://user@a:2/d",
        "tagged-scope://user@a/d",
        "tagged-scope://other@a:1/d",
    ):
        assert _tag(base / Uri(other)) == "derived-a", other
    assert _tag(base / Uri("tagged-scope://USER@a:1/d")) != "supplied"
    assert _tag(base / Uri("tagged-scope://user@A:1/d")) == "supplied"


def test_with_source_and_destinations_follow_the_same_rule():
    a = UriPath("tagged-scope://a/d/f", backend=_Backend("supplied-a"))
    assert _tag(a.with_source(a.source)) == "supplied-a"
    assert _tag(a.with_source(UriPath("tagged-scope://b/").source)) == "derived-b"
    assert _tag(a._coerce_target("tagged-scope://a/e")) == "supplied-a"
    assert _tag(a._coerce_target("tagged-scope://b/e")) == "derived-b"
    assert _tag(a._coerce_target("g")) == "supplied-a"


# --- derivations build nothing ---------------------------------------------


def test_deriving_a_path_builds_no_backend():
    p = UriPath("tagged-scope://a/x/y.txt")
    derived = [
        p.parent,
        p.parent.parent,
        *p.parents,
        p.with_name("z"),
        p.with_suffix(".t"),
        p.with_stem("s"),
        p.with_query("q=1"),
        p.with_fragment("f"),
        p.with_path("/other"),
        p.relative_to(p.parent),
        p / "k",
        p / Uri("k"),
        p / pathlib.PurePosixPath("k"),
        p.joinpath("k", Uri("m")),
        UriPath(p, "k"),
        TaggedScopePath(p, "k"),
        p._make_child_relpath("k"),
        p._coerce_target("k"),
        p._coerce_target("tagged-scope://a/e"),
        p._rename_target("k"),
        p.with_source(p.source),
    ]
    assert all(d._backend is None for d in derived)
    assert p._backend is None
    assert TaggedScopePath.built == []


def test_a_derived_path_shares_the_backend_that_exists():
    p = UriPath("tagged-scope://a/x/y.txt")
    built = p.backend
    for q in (p.parent, p.with_name("z"), p / "k", p / Uri("k"), UriPath(p, "k")):
        assert q._backend is built
        assert q.backend is built
    assert TaggedScopePath.built == ["a"]


_PURE_OPERATIONS = [
    "p.parent",
    "list(p.parents)",
    "p.with_name('z')",
    "p.with_suffix('.t')",
    "p.with_query('a=1')",
    "p.relative_to('/a')",
    "p / Uri('y')",
    "p / PurePosixPath('y')",
    "p / 'y'",
    "UriPath(p, 'y')",
]


def test_pure_operations_on_an_sftp_path_import_no_ssh_library():
    """Run in a fresh interpreter: `sys.modules` there starts without them."""
    code = "\n".join(
        [
            "import sys",
            "from pathlib import PurePosixPath",
            "from pathlib_next.uri import Uri, UriPath",
            "p = UriPath('sftp://h/a/b')",
            *(
                f"r = {operation}\n"
                f"derived = r if isinstance(r, list) else [r]\n"
                f"assert all(x._backend is None for x in derived), {operation!r}"
                for operation in _PURE_OPERATIONS
            ),
            "assert p._backend is None",
            "loaded = {'paramiko', 'asyncssh'} & set(sys.modules)",
            "assert not loaded, loaded",
            "print('ok')",
        ]
    )
    import pathlib_next

    source = str(pathlib.Path(pathlib_next.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": source, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# --- a derived backend is recorded on the path ------------------------------


class DictScopePath(UriPath):
    """A scheme whose backend is a plain dict: not weakly referenceable
    through a registry of backends, and equal to any other empty one."""

    __SCHEMES = ("dict-scope",)
    __slots__ = ()
    store = {}

    def _initbackend(self):
        return {}

    def stat(self, *, follow_symlinks=True):
        from pathlib_next.utils.stat import FileStat

        if self.path in self.store:
            return FileStat(st_mode=0o100644, st_size=len(self.store[self.path]))
        raise FileNotFoundError(self.path)

    def _open(self, mode="r", buffering=-1):
        store, key = self.store, self.path
        if "w" in mode:
            store[key] = b""

            class Writer(io.BytesIO):
                def close(self):
                    if not self.closed:
                        store[key] = self.getvalue()
                    super().close()

            return Writer()
        return io.BytesIO(store[key])

    def unlink(self, missing_ok=False):
        self.store.pop(self.path)


def test_a_derived_backend_that_is_a_dict_still_reads_as_derived():
    DictScopePath.store.clear()
    DictScopePath.store["/d/f.txt"] = b"precious"
    a, b = UriPath("dict-scope://h/d/f.txt"), UriPath("dict-scope://h/d/f.txt")
    assert a.backend == b.backend == {}
    assert a.backend is not b.backend
    assert a._supplied_backend() is None
    assert a.parent._supplied_backend() is None
    assert (a.parent / "f.txt")._supplied_backend() is None
    assert a._same_filesystem(b)
    assert _same_file(a, b)
    with pytest.raises(OSError, match="same file"):
        a.copy(b, overwrite=True)
    assert DictScopePath.store["/d/f.txt"] == b"precious"


def test_a_supplied_dict_backend_is_never_taken_for_derived():
    one, two = {}, {}
    a = UriPath("dict-scope://h/d/f.txt", backend=one)
    b = UriPath("dict-scope://h/d/f.txt", backend=two)
    assert a._supplied_backend() is one
    assert (a.parent / "f.txt")._supplied_backend() is one
    assert not a._same_filesystem(b)
    assert a.with_backend(two)._supplied_backend() is two


def test_a_backend_inherited_from_a_derived_path_is_still_derived():
    p = UriPath("dict-scope://h/d/f.txt")
    p.backend
    child = p.parent / "g"
    assert child._backend is p._backend
    assert child._supplied_backend() is None
