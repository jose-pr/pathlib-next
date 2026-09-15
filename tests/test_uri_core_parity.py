"""URI core soundness and core copy/open parity (wave 5, group G5).

Each test asserts an outcome -- the bytes a server received, the decoded
pairs, the class and URI a join produced, what exists on disk -- not merely
that nothing raised.
"""

from __future__ import annotations

import errno
import http.server
import os
import pathlib
import socket
import stat
import threading
import unittest.mock as mock

import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.uri import Uri, UriPath
from pathlib_next.uri.query import Query
from pathlib_next.uri.schemes.file import FileUri

# --- query: kept encoded, sent unchanged, decoded once (Design Q1) ---------


@pytest.fixture
def recording_http_server():
    """A loopback HTTP server that records each request line's target."""
    seen: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_HEAD = do_GET

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_request_carries_the_encoded_query_unchanged(recording_http_server):
    pytest.importorskip("requests")
    base, seen = recording_http_server
    UriPath(f"{base}/download?name=a%26b&sig=ab%2Bcd%3D%3D").read_bytes()
    # It used to go out as "name=a&b&sig=ab+cd==": a presigned signature
    # broken and an extra parameter injected.
    assert "/download?name=a%26b&sig=ab%2Bcd%3D%3D" in seen


def test_http_request_with_dict_query_is_encoded_once(recording_http_server):
    pytest.importorskip("requests")
    base, seen = recording_http_server
    UriPath(f"{base}/search").with_query({"q": "a b", "k": "x&y"}).read_bytes()
    assert "/search?q=a%20b&k=x%26y" in seen


def test_parsed_query_is_the_received_encoded_string():
    uri = Uri("http://h/download?name=a%26b&sig=ab%2Bcd%3D%3D")
    assert uri.query == "name=a%26b&sig=ab%2Bcd%3D%3D"
    assert uri.as_uri() == "http://h/download?name=a%26b&sig=ab%2Bcd%3D%3D"


def test_query_to_dict_decodes_each_value_exactly_once():
    query = Query(Uri("http://h/?q=a%26b&x=1%3D2&t=abc%2Bdef%3D&v=%2541").query)
    assert query.to_dict() == {
        "q": ["a&b"],
        "x": ["1=2"],
        "t": ["abc+def="],
        "v": ["%41"],
    }


def test_query_key_containing_equals_round_trips():
    assert Query({"a=b": "c"}).decode() == [("a=b", "c")]
    assert Query([("k=", "v")]).to_dict() == {"k=": ["v"]}


def test_query_characters_illegal_on_the_wire_are_encoded():
    assert Uri("http://h/?q=a b").as_uri() == "http://h/?q=a%20b"
    assert Uri("http://h/?p=100%").as_uri() == "http://h/?p=100%25"


# --- remote / relative concrete Path keeps the remote (Design Q2) ----------


@pytest.mark.parametrize(
    "relative",
    [pathlib.Path("etc/x"), pathlib_next.LocalPath("etc/x")],
    ids=["pathlib.Path", "LocalPath"],
)
def test_remote_join_with_relative_concrete_path_stays_remote(relative):
    pytest.importorskip("paramiko")
    joined = UriPath("sftp://root@host/srv/") / relative
    assert type(joined).__name__ == "SftpPath"
    assert joined.as_uri() == "sftp://root@host/srv/etc/x"
    assert UriPath("sftp://root@host/srv/").joinpath(relative) == joined


def test_remote_join_with_absolute_concrete_path_is_a_local_file():
    local = pathlib.Path("etc/x").absolute()
    joined = UriPath("http://h/base/") / local
    assert isinstance(joined, FileUri)
    assert pathlib.Path(os.fspath(joined)) == local
    # joinpath dispatches the class from the scheme, as `/` does.
    assert isinstance(UriPath("http://h/base/").joinpath(local), FileUri)


def test_relative_concrete_path_alone_is_still_a_local_file():
    uri = UriPath(pathlib.Path("rel/x.txt"))
    assert isinstance(uri, FileUri)
    assert uri.path == "rel/x.txt"


# --- file: drive and UNC forms (Windows) -----------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows drive letters")
def test_file_localhost_drive_uri_renders_and_hashes():
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    drive_path = pathlib.Path(windir).as_posix()
    uri = UriPath(f"file://localhost/{drive_path}")
    assert str(uri) == f"file://localhost/{drive_path}"
    assert repr(uri) == f"FileUri('file://localhost/{drive_path}')"
    assert uri in {uri}
    assert os.fspath(uri) == drive_path
    assert uri.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows drive letters")
def test_file_uri_of_local_path_matches_the_pure_uri(tmp_path):
    local = pathlib_next.LocalPath(tmp_path)
    assert Uri(local) == UriPath(local)
    assert str(UriPath(local)) == str(Uri(local))
    assert pathlib.Path(os.fspath(UriPath(local))) == tmp_path


@pytest.mark.skipif(os.name != "nt", reason="UNC paths")
def test_file_uri_naming_this_host_is_a_unc_share_not_a_local_folder():
    hostname = socket.gethostname()
    assert (
        os.fspath(UriPath(f"file://{hostname}/share/x.txt"))
        == f"//{hostname.lower()}/share/x.txt"
    )
    assert os.fspath(UriPath("file://127.0.0.1/c$/Windows")) == "//127.0.0.1/c$/Windows"


# --- scheme map: late registration and cached misses -----------------------


def test_scheme_class_defined_after_first_dispatch_is_found():
    UriPath("file:/a")  # the map is built and cached

    class LateG5Path(UriPath):
        __SCHEMES = ("late-g5",)

    assert type(UriPath("late-g5://h/a")) is LateG5Path


def test_unregistered_scheme_scans_entry_points_once():
    calls = []

    def entry_points(*args, **kwargs):
        calls.append(kwargs)
        return [] if kwargs.get("group") else {}

    with mock.patch("importlib.metadata.entry_points", side_effect=entry_points):
        for _ in range(3):
            assert type(UriPath("nosuch-g5:x")) is UriPath
    assert len(calls) == 1


# --- non-UTF-8 percent-escapes ----------------------------------------------


def test_non_utf8_escape_constructs_and_round_trips():
    uri = Uri("http://h/caf%E9.html?x=%FF#fr%E9")
    assert uri.as_uri() == "http://h/caf%E9.html?x=%FF#fr%E9"
    assert uri.name.encode("utf-8", "surrogateescape") == b"caf\xe9.html"


def test_non_utf8_escape_constructs_a_scheme_path():
    pytest.importorskip("requests")
    path = UriPath("http://h/caf%E9.html")
    assert type(path).__name__ == "HttpPath"
    assert str(path) == "http://h/caf%E9.html"


# --- copy(): symlinks, mode bits, own subtree --------------------------------


def _symlink_or_skip(link, target, target_is_directory=False):
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")


def test_copy_without_following_a_symlink_copies_the_link(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    _symlink_or_skip(tmp_path / "link", secret)
    link = pathlib_next.LocalPath(tmp_path / "link")
    out = pathlib_next.LocalPath(tmp_path / "out")

    link.copy(out, follow_symlinks=False)

    # pathlib 3.14: a symlink to the same target, not a regular file holding
    # the target's content.
    assert out.is_symlink()
    assert os.readlink(out) == os.readlink(link)


def test_copy_without_following_refuses_to_replace_without_overwrite(tmp_path):
    (tmp_path / "t.txt").write_text("t")
    _symlink_or_skip(tmp_path / "link", tmp_path / "t.txt")
    (tmp_path / "out").write_text("keep")
    link = pathlib_next.LocalPath(tmp_path / "link")
    with pytest.raises(FileExistsError):
        link.copy(pathlib_next.LocalPath(tmp_path / "out"), follow_symlinks=False)
    assert (tmp_path / "out").read_text() == "keep"


def test_recursive_copy_without_following_keeps_inner_links(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_text("r")
    _symlink_or_skip(src / "alias.txt", "real.txt")
    dst = pathlib_next.LocalPath(tmp_path / "dst")

    pathlib_next.LocalPath(src).copy(dst, recursive=True, follow_symlinks=False)

    assert (dst / "real.txt").read_text() == "r"
    assert (dst / "alias.txt").is_symlink()
    assert os.readlink(dst / "alias.txt") == "real.txt"


def test_copy_preserve_metadata_passes_permission_bits_only():
    calls = []

    class Source(MemPath):
        def stat(self, *, follow_symlinks=True):
            st = super().stat(follow_symlinks=follow_symlinks)
            st.setmode(0o640)
            return st

    class Target(MemPath):
        def chmod(self, mode, *, follow_symlinks=True):
            calls.append(mode)

    src = Source("/a.txt")
    src.write_text("x")
    src.copy(Target("/b.txt"))
    # Not 0o100640: the file-type bits are not a mode (FTP's SITE CHMOD sent
    # them verbatim).
    assert calls == [0o640]


def test_copy_directory_into_its_own_subtree_raises_before_creating():
    root = MemPath("/d")
    root.mkdir()
    (root / "a.txt").write_text("x")
    with pytest.raises(OSError) as excinfo:
        root.copy(root / "inner", recursive=True)
    assert excinfo.value.errno == errno.EINVAL
    assert [p.name for p in root.iterdir()] == ["a.txt"]
    with pytest.raises(OSError):
        root.copy(root, recursive=True, overwrite=True)


def test_local_copy_directory_into_its_own_subtree_raises(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.txt").write_text("x")
    root = pathlib_next.LocalPath(tmp_path / "d")
    with pytest.raises(OSError) as excinfo:
        root.copy(root / "sub" / "inner", recursive=True)
    assert excinfo.value.errno == errno.EINVAL
    assert sorted(os.listdir(tmp_path / "d")) == ["a.txt"]


def test_copy_into_a_sibling_with_a_shared_prefix_is_allowed():
    root = MemPath("/d")
    root.mkdir()
    (root / "a.txt").write_text("x")
    sibling = MemPath("/d2", backend=root.backend)
    root.copy(sibling, recursive=True)
    assert (sibling / "a.txt").read_text() == "x"


# --- open(): mode validation like open() -------------------------------------


@pytest.mark.parametrize(
    "mode, kwargs",
    [
        ("zz", {}),
        ("rw", {}),
        ("rbt", {}),
        ("rr", {}),
        ("b", {}),
        ("rb", {"encoding": "utf-8"}),
        ("rb", {"errors": "strict"}),
        ("rb", {"newline": ""}),
    ],
)
def test_invalid_open_mode_raises_valueerror_like_pathlib(tmp_path, mode, kwargs):
    real = tmp_path / "f.txt"
    real.write_text("x")
    with pytest.raises(ValueError):
        real.open(mode, **kwargs)
    mem = MemPath("/f.txt")
    mem.write_text("x")
    with pytest.raises(ValueError):
        mem.open(mode, **kwargs)


def test_text_mode_letter_is_accepted():
    mem = MemPath("/f.txt")
    with mem.open("wt", encoding="utf-8") as handle:
        handle.write("hé")
    with mem.open("rt", encoding="utf-8") as handle:
        assert handle.read() == "hé"
    with mem.open("at", encoding="utf-8") as handle:
        handle.write("!")
    assert mem.read_bytes() == "hé!".encode()


def test_valid_but_unsupported_mode_stays_not_implemented():
    mem = MemPath("/f.txt")
    mem.write_text("x")
    with pytest.raises(NotImplementedError):
        mem.open("r+")


def test_data_uri_payload_is_opaque_and_decoded_once():
    assert UriPath("data:,a/./b").read_bytes() == b"a/./b"
    assert UriPath("data:text/plain,x/../y").read_bytes() == b"x/../y"
    assert UriPath("data:,100%2525").read_bytes() == b"100%25"
    binary = UriPath("data:application/octet-stream,%FF%FE")
    assert binary.read_bytes() == b"\xff\xfe"
    assert str(binary) == "data:application/octet-stream,%FF%FE"


def test_stat_mode_constant_is_what_chmod_receives_for_local_copy(tmp_path):
    src = pathlib_next.LocalPath(tmp_path / "s.txt")
    src.write_text("x")
    received = []

    class Recording(pathlib_next.LocalPath):
        __slots__ = ()

        def chmod(self, mode, *, follow_symlinks=True):
            received.append(mode)
            return super().chmod(mode, follow_symlinks=follow_symlinks)

    src.copy(Recording(tmp_path / "t.txt"))
    assert received == [stat.S_IMODE(os.stat(src).st_mode)]
