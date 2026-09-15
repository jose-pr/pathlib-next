"""Regressions for low-severity soundness defects in the core `Path`
protocols, `MemPath`, `FileStat`, checksums and the URI core."""

from __future__ import annotations

import errno
import hashlib
import inspect
import io
import os
import pathlib
import sys
import threading
import types

import pytest

from pathlib_next import LocalPath
from pathlib_next.mempath import MemPath, MemPathBackend
from pathlib_next.protocols import BinaryOpen
from pathlib_next.uri import Uri, UriPath
from pathlib_next.uri.schemes.data import DataUri
from pathlib_next.uri.schemes.file import FileUri
from pathlib_next.uri.source import Source
from pathlib_next.utils import checksum
from pathlib_next.utils.stat import FileStat

# --- open(): handle cleanup, mode validation, line buffering ----------------


class _RecordingMemPath(MemPath):
    __slots__ = ()
    handles: list = []

    def _open(self, mode="r", buffering=-1):
        handle = super()._open(mode, buffering)
        _RecordingMemPath.handles.append(handle)
        return handle


@pytest.mark.parametrize(
    "kwargs, error",
    [({"encoding": "no-such-codec"}, LookupError), ({"newline": "bogus"}, ValueError)],
)
def test_open_closes_backend_handle_when_text_wrapper_fails(kwargs, error):
    path = _RecordingMemPath("/f.txt")
    path.write_bytes(b"x")
    _RecordingMemPath.handles.clear()
    with pytest.raises(error):
        path.open("r", **kwargs)
    assert len(_RecordingMemPath.handles) == 1
    assert _RecordingMemPath.handles[0].closed is True


@pytest.mark.parametrize(
    "mode, kwargs",
    [
        ("bt", {}),
        ("rw", {}),
        ("zz", {}),
        ("rr", {}),
        ("rb", {"encoding": "utf-8"}),
        ("rb", {"errors": "strict"}),
        ("rb", {"newline": ""}),
        ("r", {"buffering": 0}),
    ],
)
def test_open_rejects_invalid_modes_like_builtin_open(tmp_path, mode, kwargs):
    local = tmp_path / "f.txt"
    local.write_bytes(b"x")
    with pytest.raises(ValueError):
        open(local, mode, **kwargs)
    path = MemPath("/f.txt")
    path.write_bytes(b"x")
    with pytest.raises(ValueError):
        path.open(mode, **kwargs)


def test_open_text_buffering_one_is_line_buffered():
    path = MemPath("/lines.txt")
    with path.open("w", buffering=1) as handle:
        assert handle.line_buffering is True
        handle.write("a\n")
        # Line buffering pushes the line to the binary handle, whose flush
        # publishes it.
        assert path.read_text() == "a\n"
    with path.open("r") as handle:
        assert handle.line_buffering is False


# --- synthesized OSErrors carry errno and filename ---------------------------


def _raised(call):
    with pytest.raises(OSError) as info:
        call()
    return info.value


def test_mempath_errors_carry_errno_and_filename():
    root = MemPath("/")
    (root / "d").mkdir()
    (root / "f").write_bytes(b"x")
    cases = [
        (lambda: MemPath("/missing", backend=root.backend).rm(), errno.ENOENT),
        (lambda: (root / "missing").unlink(), errno.ENOENT),
        (lambda: (root / "missing").stat(), errno.ENOENT),
        (lambda: (root / "missing").open("rb"), errno.ENOENT),
        (lambda: (root / "missing").rmdir(), errno.ENOENT),
        (lambda: list((root / "missing").iterdir()), errno.ENOENT),
        (lambda: (root / "f").touch(exist_ok=False), errno.EEXIST),
        (lambda: (root / "d").mkdir(), errno.EEXIST),
        (lambda: (root / "f").open("xb"), errno.EEXIST),
        (lambda: (root / "d").unlink(), errno.EISDIR),
        (lambda: (root / "d").open("rb"), errno.EISDIR),
        (lambda: list((root / "f").iterdir()), errno.ENOTDIR),
        (lambda: (root / "f").rmdir(), errno.ENOTDIR),
    ]
    for call, code in cases:
        error = _raised(call)
        assert error.errno == code, (error, code)
        assert isinstance(error.filename, str) and error.filename, error


def test_generic_rm_missing_error_is_enoent_with_filename():
    error = _raised(lambda: MemPath("/missing").rm())
    assert isinstance(error, FileNotFoundError)
    assert (error.errno, error.filename) == (errno.ENOENT, "/missing")


def test_localpath_rm_missing_error_is_enoent(tmp_path):
    missing = LocalPath(tmp_path / "missing")
    error = _raised(missing.rm)
    assert isinstance(error, FileNotFoundError)
    assert error.errno == errno.ENOENT
    assert error.filename == str(missing)


# --- copy(progress=) on an empty file ---------------------------------------


def test_path_copy_progress_fires_once_for_empty_file():
    source = MemPath("/empty")
    source.write_bytes(b"")
    calls = []
    source.copy(
        source.with_segments("/copy"),
        progress=lambda path, copied, total: calls.append((path, copied, total)),
    )
    assert calls == [(source, 0, 0)]


def test_binaryopen_copy_progress_fires_once_for_empty_file():
    source = MemPath("/empty")
    source.write_bytes(b"")
    calls = []
    BinaryOpen.copy(
        source,
        source.with_segments("/copy"),
        progress=lambda copied, total: calls.append((copied, total)),
    )
    assert calls == [(0, 0)]


def test_copy_progress_for_nonempty_file_has_no_extra_zero_call():
    source = MemPath("/data")
    source.write_bytes(b"abc")
    calls = []
    source.copy(
        source.with_segments("/copy"),
        progress=lambda path, copied, total: calls.append((copied, total)),
    )
    assert calls == [(3, 3)]


# --- samefile() keeps the backend / host of a str argument ------------------


class _IdentityMemPath(MemPath):
    """A MemPath whose stat() reports a (st_dev, st_ino) identity."""

    __slots__ = ()

    def stat(self, *, follow_symlinks=True):
        parent, name = self._parent_container()
        if name not in parent:
            raise FileNotFoundError(errno.ENOENT, "missing", str(self))
        return types.SimpleNamespace(
            st_mode=0o100644, st_dev=id(self.backend), st_ino=id(parent[name])
        )


def test_samefile_str_resolves_on_the_same_backend():
    path = _IdentityMemPath("/a")
    path.write_bytes(b"1")
    path.with_segments("/b").write_bytes(b"2")
    # A fresh backend (the bare constructor) raised FileNotFoundError here.
    assert path.samefile("/a") is True
    assert path.samefile("/b") is False


def test_uripath_samefile_str_keeps_the_host(monkeypatch):
    seen = []

    def fake_stat(self, *, follow_symlinks=True):
        seen.append((self.source.host, self.path))
        return types.SimpleNamespace(st_mode=0o100644, st_dev=1, st_ino=self.path)

    monkeypatch.setattr(FileUri, "stat", fake_stat)
    path = UriPath("file://server/share/a")
    assert path.samefile("/share/a") is True
    assert seen == [("server", "/share/a"), ("server", "/share/a")]


# --- MemPath file handles behave like real files ----------------------------


def test_mempath_append_writes_at_end_after_seek():
    path = MemPath("/f")
    path.write_bytes(b"x")
    with path.open("ab") as handle:
        handle.seek(0)
        handle.write(b"Q")
        handle.writelines([b"R", b"S"])
    assert path.read_bytes() == b"xQRS"


def test_mempath_read_handle_is_not_writable():
    path = MemPath("/f")
    path.write_bytes(b"data")
    with path.open("rb") as handle:
        assert handle.writable() is False
        with pytest.raises(io.UnsupportedOperation):
            handle.write(b"zz")
        with pytest.raises(io.UnsupportedOperation):
            handle.truncate(0)
        assert handle.read() == b"data"
    assert path.read_bytes() == b"data"


def test_mempath_handle_double_close_is_a_no_op():
    path = MemPath("/f")
    handle = path.open("wb")
    handle.write(b"1")
    handle.close()
    handle.close()
    assert path.read_bytes() == b"1"


def test_mempath_flush_publishes_written_bytes():
    path = MemPath("/f")
    with path.open("w") as handle:
        handle.write("hello")
        handle.flush()
        assert path.read_text() == "hello"


def _race(action, trials=200):
    """Run `action(i)` on two threads at once per trial; return how many
    trials let both succeed."""
    both = 0
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for trial in range(trials):
            barrier = threading.Barrier(2)
            results = []

            def worker():
                barrier.wait()
                try:
                    action(trial)
                    results.append(True)
                except FileExistsError:
                    results.append(False)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert sorted(results) in ([False, True], [True, True])
            both += results == [True, True]
    finally:
        sys.setswitchinterval(interval)
    return both


def test_mempath_exclusive_create_is_atomic():
    backend = MemPathBackend()
    assert _race(lambda i: MemPath(f"/f{i}", backend=backend).open("xb").close()) == 0


def test_mempath_mkdir_is_atomic():
    backend = MemPathBackend()
    assert _race(lambda i: MemPath(f"/d{i}", backend=backend).mkdir()) == 0


def test_mempath_unlink_root_raises_even_with_missing_ok():
    with pytest.raises(IsADirectoryError):
        MemPath("/").unlink(missing_ok=True)
    with pytest.raises(IsADirectoryError):
        MemPath("/").unlink()


# --- FileStat.from_stat with None fields ------------------------------------


def test_filestat_from_stat_maps_none_fields_to_zero():
    foreign = types.SimpleNamespace(
        st_mode=None, st_size=None, st_uid=None, st_gid=None, st_mtime=None
    )
    stat = FileStat.from_stat(foreign)
    assert dict(stat.items()) == {field: 0 for field in FileStat._FIELDS}
    assert stat.is_dir() is False
    assert stat.mode_known is False
    assert repr(stat) == "<FileStat mode=0, size=0, mtime=0>"


def test_filestat_from_paramiko_attributes_without_flags():
    paramiko = pytest.importorskip("paramiko")
    stat = FileStat.from_stat(paramiko.SFTPAttributes())
    assert (stat.st_mode, stat.st_size, stat.st_mtime) == (0, 0, 0)
    assert stat.is_dir() is False and stat.is_file() is False


# --- checksums do not claim a security use ----------------------------------


def test_checksums_work_when_security_md5_is_disabled(monkeypatch):
    real_new = hashlib.new

    def fips_new(name, *args, **kwargs):
        # A FIPS-mode OpenSSL rejects md5 unless usedforsecurity=False.
        if name.lower() == "md5" and kwargs.get("usedforsecurity", True):
            raise ValueError("[digital envelope routines] unsupported")
        return real_new(name, *args, **kwargs)

    def fips_md5(*args, **kwargs):
        return fips_new("md5", *args, **kwargs)

    monkeypatch.setattr(hashlib, "new", fips_new)
    monkeypatch.setattr(hashlib, "md5", fips_md5)
    path = MemPath("/f")
    path.write_bytes(b"abc")
    expected = real_new("md5", b"abc").hexdigest()
    assert checksum.md5(path) == expected
    assert checksum.stream(path, "md5") == expected
    assert checksum.sha256(path) == real_new("sha256", b"abc").hexdigest()


# --- is_dir()/is_file() accept follow_symlinks= (3.13 parity) ---------------


@pytest.mark.parametrize("name", ["is_dir", "is_file"])
@pytest.mark.parametrize("cls", [MemPath, LocalPath, UriPath])
def test_is_dir_is_file_signature_has_follow_symlinks(cls, name):
    parameter = inspect.signature(getattr(cls, name)).parameters["follow_symlinks"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is True


def test_mempath_is_dir_is_file_follow_symlinks_keyword():
    root = MemPath("/")
    (root / "d").mkdir()
    (root / "f").write_bytes(b"")
    assert (root / "d").is_dir(follow_symlinks=False) is True
    assert (root / "f").is_file(follow_symlinks=False) is True
    assert (root / "f").is_dir(follow_symlinks=False) is False
    assert (root / "missing").is_file(follow_symlinks=False) is False


def test_mempath_is_dir_forwards_follow_symlinks_to_stat():
    calls = []

    class Spy(MemPath):
        __slots__ = ()

        def stat(self, *, follow_symlinks=True):
            calls.append(follow_symlinks)
            return FileStat(st_mode=0o120777)

    Spy("/x").is_dir(follow_symlinks=False)
    Spy("/x").is_file()
    assert calls == [False, True]


def test_localpath_is_dir_follow_symlinks_false_on_link(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "f").write_bytes(b"")
    link_dir = LocalPath(tmp_path / "link_dir")
    link_file = LocalPath(tmp_path / "link_file")
    try:
        link_dir.symlink_to(target, target_is_directory=True)
        link_file.symlink_to(target / "f")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"cannot create symlinks here: {error}")
    stdlib = pathlib.Path(link_dir)
    assert link_dir.is_dir() is True
    assert link_dir.is_dir(follow_symlinks=False) is False
    assert link_file.is_file() is True
    assert link_file.is_file(follow_symlinks=False) is False
    if sys.version_info >= (3, 13):
        assert stdlib.is_dir(follow_symlinks=False) is False
    assert LocalPath(tmp_path / "missing").is_dir(follow_symlinks=False) is False


# --- pure-path gaps: str / path, 3.14 trailing-dot suffixes -----------------


def test_str_rtruediv_mempath_keeps_backend_and_restarts_on_absolute():
    base = MemPath("b")
    joined = "a" / base
    assert isinstance(joined, MemPath)
    assert joined.as_posix() == "a/b"
    assert joined.backend is base.backend
    absolute = MemPath("/b")
    assert ("a" / absolute).as_posix() == "/b"
    assert ("a" / absolute).backend is absolute.backend
    with pytest.raises(TypeError):
        3 / base


def test_str_rtruediv_uri_is_a_decoded_path():
    joined = "a" / Uri("b")
    assert type(joined) is Uri
    assert joined.path == "a/b"
    # Not URI syntax: no scheme, query or fragment is parsed out of the str.
    odd = "C:/x?y#z" / Uri("b")
    assert (odd.source, odd.path, odd.query, odd.fragment) == (
        Source(None, None, None, None),
        "C:/x?y#z/b",
        None,
        None,
    )


def test_str_rtruediv_uripath_keeps_source_and_backend():
    backend = object()
    base = UriPath("http://h/b").with_backend(backend)
    joined = "a" / base
    assert type(joined) is type(base)
    # An absolute path restarts the join, as in pathlib.
    assert joined.as_uri() == "http://h/b"
    assert joined.backend is backend
    relative = "C:/p" / UriPath("x")
    assert (type(relative), relative.source, relative.path) == (
        UriPath,
        Source(None, None, None, None),
        "C:/p/x",
    )


_SUFFIX_NAMES = [
    "a",
    "a.b",
    "a.b.c",
    "a.",
    "a..",
    "..a",
    ".a.",
    "...",
    "a.b.",
    "a..b",
    ".a",
    ".a.b",
    "a.b..",
    "..a.b",
]


@pytest.mark.parametrize("name", _SUFFIX_NAMES)
def test_suffix_rules_follow_running_pathlib(name):
    expected = pathlib.PurePosixPath("x", name)
    for path in (MemPath("x", name), Uri(f"x/{name}"), UriPath(f"http://h/x/{name}")):
        assert path.name == expected.name
        assert path.suffix == expected.suffix, path
        assert path.suffixes == expected.suffixes, path
        assert path.stem == expected.stem, path
        assert path.with_suffix(".t").name == expected.with_suffix(".t").name, path


# --- explicit schemesmap is authoritative -----------------------------------


def test_explicit_schemesmap_is_not_extended_on_a_miss():
    assert type(UriPath("ftp://h/a", schemesmap={"data": DataUri})) is UriPath
    assert type(UriPath("file:///etc/x", schemesmap={"data": DataUri})) is UriPath
    assert type(UriPath("data:,x", schemesmap={"data": DataUri})) is DataUri
    assert type(UriPath("data:,x", schemesmap={})) is UriPath
    # Without a map, the registry (and lazy loading) still applies.
    assert type(UriPath("data:,x")) is DataUri


# --- lazy parse publishes a fully-initialised Uri ---------------------------


def test_lazy_parse_sets_initiated_after_the_components():
    observed = []

    class Probe(Uri):
        __slots__ = ()

        def __setattr__(self, name, value):
            if name in ("_source", "_path", "_query", "_fragment"):
                observed.append((name, bool(getattr(self, "_initiated", None))))
            object.__setattr__(self, name, value)

    probe = Probe("http://h/a?q#f")
    observed.clear()
    assert probe.path == "/a"
    assert [name for name, _ in observed] == ["_source", "_path", "_query", "_fragment"]
    assert not any(initiated for _, initiated in observed)


# --- Uri equality and hashing -----------------------------------------------


def test_uri_eq_against_relative_localpath_does_not_raise():
    assert (LocalPath("a") in [Uri("x")]) is False
    assert (Uri("x") == LocalPath("a")) is False


def test_uri_eq_is_consistent_with_hash():
    mem = MemPath("/a")
    uri = Uri("mempath:/a")
    assert uri != mem and mem != uri
    assert len({uri, mem}) == 2
    assert uri == "mempath:/a" and hash(uri) == hash("mempath:/a")
    other = UriPath("mempath:/a")
    assert uri == other and hash(uri) == hash(other)


# --- with_source() without a scheme; `/` does not mask internal errors ------


def test_with_source_without_scheme_gives_plain_uripath():
    moved = UriPath("http://h/a").with_source(Source(None, None, "other", None))
    assert type(moved) is UriPath
    assert (moved.source.host, moved.path) == ("other", "/a")


def test_truediv_does_not_mask_a_scheme_type_error(monkeypatch):
    base = UriPath("data:,a")

    def broken_init(self, *args, **kwargs):
        raise TypeError("scheme bug")

    monkeypatch.setattr(DataUri, "_init", broken_init)
    with pytest.raises(TypeError, match="scheme bug"):
        base / "b"


def test_truediv_unsupported_operand_still_returns_notimplemented():
    base = UriPath("http://h/a")
    assert base.__truediv__(3) is NotImplemented
    with pytest.raises(TypeError, match="unsupported operand"):
        base / 3
    assert (base / pathlib.PurePosixPath("b")).path == "/a/b"


# --- paths starting with "//" -----------------------------------------------


def test_posix_double_slash_path_is_not_an_authority():
    uri = Uri(pathlib.PurePosixPath("//a/b"))
    assert not uri.source
    assert uri.path == "//a/b"
    assert Uri(str(uri)).path == "//a/b"


def test_file_uri_with_empty_authority_and_double_slash_path_renders():
    uri = Uri("file:////server/share/x")
    text = str(uri)
    reparsed = Uri(text)
    assert (reparsed.source.scheme, reparsed.path) == ("file", "//server/share/x")
    assert reparsed == uri and hash(reparsed) == hash(uri)
    assert repr(uri)


# --- internationalized host names -------------------------------------------


def test_non_ascii_host_is_composed_as_idna():
    path = UriPath("http://b\u00fccher.example/a")
    assert path.source.host == "b\u00fccher.example"
    assert path.as_uri() == "http://xn--bcher-kva.example/a"
    assert UriPath(path.as_uri()) == path


def test_non_ascii_host_request_url_is_idna():
    requests = pytest.importorskip("requests")
    url = UriPath("https://b\u00fccher.example/a").as_uri()
    prepared = requests.Request("GET", url).prepare()
    assert prepared.url == "https://xn--bcher-kva.example/a"


def test_invalid_idna_host_keeps_percent_encoding():
    # An empty label cannot be IDNA-encoded.
    assert Uri("http://b\u00fc..x/a").as_uri() == "http://b%C3%BC..x/a"


# --- data: URIs -------------------------------------------------------------


@pytest.mark.parametrize("mode", ["r+b", "w", "ab", "x"])
def test_data_uri_rejects_every_writable_mode(mode):
    with pytest.raises(NotImplementedError):
        UriPath("data:,abc").open(mode)


def test_data_uri_read_handle_is_not_writable():
    with UriPath("data:,abc").open("rb") as handle:
        assert handle.writable() is False
        with pytest.raises(io.UnsupportedOperation):
            handle.write(b"zz")
        assert handle.read() == b"abc"


@pytest.mark.parametrize(
    "uri, mediatype, content",
    [
        ("data:;charset=utf-8,x", "text/plain;charset=utf-8", b"x"),
        ("data:;base64,YWJj", "text/plain;charset=US-ASCII", b"abc"),
        ("data:;charset=utf-8;base64,YWJj", "text/plain;charset=utf-8", b"abc"),
        ("data:image/png;base64,YWJj", "image/png", b"abc"),
        # No ";": "base64" is the (malformed) media type, not the encoding.
        ("data:base64,YWJj", "base64", b"YWJj"),
    ],
)
def test_data_uri_mediatype_and_base64_follow_rfc2397(uri, mediatype, content):
    path = UriPath(uri)
    assert path.mediatype == mediatype
    assert path.read_bytes() == content


# --- is_absolute() ----------------------------------------------------------


def test_uri_is_absolute_follows_the_path():
    assert Uri("/a").is_absolute() is True
    assert Uri("a").is_absolute() is False
    assert Uri("").is_absolute() is False
    assert Uri("http://h/a").is_absolute() is True
    assert UriPath("data:,abc").is_absolute() is False


def test_file_uri_of_absolute_local_path_is_absolute(tmp_path):
    uri = UriPath(LocalPath(tmp_path))
    assert isinstance(uri, FileUri)
    assert uri.is_absolute() is True


@pytest.mark.skipif(os.name != "nt", reason="drive letters are Windows-only")
def test_file_uri_drive_relative_is_not_absolute():
    assert FileUri("file:///C:/x").is_absolute() is True
    assert FileUri("file:///C:/").is_absolute() is True
    assert FileUri("file:C:").is_absolute() is False
