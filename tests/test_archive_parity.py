"""Archive scheme and archive utility parity regressions (2026-09-15 deep
review, wave 5 / G1).

Each test asserts an outcome a caller relies on: paths listed from the
archive root exist and read back, a URI string names the member it came
from, errors carry pathlib's exception types, writes and stored modes are
not lost, and `make_archive`/`unpack_archive` work for any `Path`.
"""

import io
import os
import shutil
import stat
import tarfile
import warnings
import zipfile

import pytest

from pathlib_next import LocalPath
from pathlib_next.mempath import MemPath
from pathlib_next.uri import UriPath
from pathlib_next.utils import make_archive, unpack_archive


def _zip_uri(path, inner=""):
    return f"zip:{path.as_uri()}!/{inner}"


def _tar_uri(path, inner=""):
    return f"tar:{path.as_uri()}!/{inner}"


@pytest.fixture
def zip_archive(tmp_path):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("docs/readme.txt", "hello world")
        zf.writestr("top.txt", "top level")
    return path


@pytest.fixture
def tar_archive(tmp_path):
    path = tmp_path / "a.tar"
    with tarfile.open(path, "w") as tf:
        for name, data in [
            ("docs/readme.txt", b"hello world"),
            ("top.txt", b"top level"),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture(params=["zip", "tar"])
def archive_root(request, zip_archive, tar_archive):
    if request.param == "zip":
        return UriPath(_zip_uri(zip_archive))
    return UriPath(_tar_uri(tar_archive))


def _tree(local):
    return sorted(
        (p.relative_to(local).as_posix(), p.read_bytes() if p.is_file() else None)
        for p in local.rglob("*")
    )


_EXPECTED_TREE = [
    ("docs", None),
    ("docs/readme.txt", b"hello world"),
    ("top.txt", b"top level"),
]


# --- ftparchive-archive-root-children-leading-slash ---


def test_root_children_exist_and_read(archive_root):
    children = {child.name: child for child in archive_root.iterdir()}
    assert sorted(children) == ["docs", "top.txt"]
    assert all(child.exists() for child in children.values())
    assert children["top.txt"].read_text() == "top level"
    assert children["docs"].is_dir()
    assert [p.name for p in children["docs"].iterdir()] == ["readme.txt"]


def test_root_glob_and_rglob_find_every_member(archive_root):
    assert sorted(p.path for p in archive_root.glob("*")) == ["docs", "top.txt"]
    found = {p.path: p.read_text() for p in archive_root.rglob("*.txt")}
    assert found == {"docs/readme.txt": "hello world", "top.txt": "top level"}


def test_root_recursive_copy_copies_the_whole_tree(archive_root, tmp_path):
    dest = tmp_path / "out"
    archive_root.copy(LocalPath(dest), recursive=True)
    assert _tree(dest) == _EXPECTED_TREE


# --- ftparchive-tar-dotslash-members ---


@pytest.fixture
def dotslash_tgz(tmp_path):
    src = tmp_path / "site"
    (src / "docs").mkdir(parents=True)
    (src / "docs" / "readme.txt").write_bytes(b"hello world")
    (src / "top.txt").write_bytes(b"top level")
    # The stdlib's own default tarball layout: '.', './docs', './top.txt'.
    base = shutil.make_archive(str(tmp_path / "site"), "gztar", root_dir=src)
    with tarfile.open(base) as tf:
        assert "./top.txt" in tf.getnames()
    return LocalPath(base)


def test_tar_dotslash_members_are_addressable(dotslash_tgz, tmp_path):
    root = UriPath(_tar_uri(dotslash_tgz))
    assert sorted(p.name for p in root.iterdir()) == ["docs", "top.txt"]
    assert (root / "top.txt").read_text() == "top level"
    assert (root / "docs").is_dir()
    assert UriPath(_tar_uri(dotslash_tgz, "docs/readme.txt")).read_bytes() == (
        b"hello world"
    )
    dest = tmp_path / "out"
    root.copy(LocalPath(dest), recursive=True)
    assert _tree(dest) == _EXPECTED_TREE


# --- ftparchive-archive-uri-not-roundtrippable ---

_ODD_NAMES = ["a#1.txt", "b?.txt", "c%41.txt", "cA.txt", "d e.txt"]


def test_member_uri_round_trips_to_the_same_member(tmp_path):
    path = tmp_path / "odd.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name in _ODD_NAMES:
            zf.writestr(f"d/{name}", name)
    children = list(UriPath(_zip_uri(path, "d")).iterdir())
    assert sorted(c.name for c in children) == sorted(_ODD_NAMES)
    for child in children:
        for text in (child.as_uri(), str(child)):
            again = UriPath(text)
            assert again.path == child.path
            assert again.read_text() == child.name
            assert again.as_uri() == child.as_uri()


def test_outer_archive_name_with_uri_delimiters(tmp_path):
    path = tmp_path / "we#ird %41.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("x.txt", "inside")
    member = UriPath(_zip_uri(path, "x.txt"))
    assert member.read_text() == "inside"
    assert UriPath(member.as_uri()).read_text() == "inside"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["we#ird %41.zip"]


def test_outer_query_stays_with_the_outer_uri():
    member = UriPath("zip:https://example.invalid/a.zip?sig=1!/docs/x.txt")
    assert member.path == "docs/x.txt"
    assert not member.query
    outer = member.backend.outer
    assert outer.path == "/a.zip"
    assert str(outer.query) == "sig=1"
    assert UriPath(member.as_uri()).path == "docs/x.txt"


def test_inner_dot_segments_never_change_the_outer_archive(zip_archive):
    member = UriPath(_zip_uri(zip_archive, "../../elsewhere/../top.txt"))
    assert member.backend.outer.name == "a.zip"
    assert member.path == "top.txt"
    assert member.read_text() == "top level"


def test_relative_to_gives_a_relative_path_not_a_none_scheme(zip_archive):
    readme = UriPath(_zip_uri(zip_archive)) / "docs" / "readme.txt"
    rel = readme.relative_to(readme.parent)
    assert str(rel) == "readme.txt"
    assert "None" not in repr(rel)


# --- ftparchive-archive-exception-parity ---


def test_iterdir_on_a_file_or_missing_path(archive_root):
    with pytest.raises(NotADirectoryError):
        list((archive_root / "top.txt").iterdir())
    with pytest.raises(FileNotFoundError):
        list((archive_root / "missing").iterdir())


def test_reading_a_directory_raises_is_a_directory(archive_root):
    with pytest.raises(IsADirectoryError):
        (archive_root / "docs").read_bytes()
    with pytest.raises(IsADirectoryError):
        archive_root.read_bytes()


def _names(path):
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def test_zip_mutations_check_the_type_of_the_path(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    before = _names(zip_archive)
    with pytest.raises(IsADirectoryError):
        (root / "docs").unlink()
    with pytest.raises(IsADirectoryError):
        (root / "docs").unlink(missing_ok=True)
    with pytest.raises(NotADirectoryError):
        (root / "top.txt").rmdir()
    with pytest.raises(IsADirectoryError):
        (root / "docs").write_text("shadow")
    assert _names(zip_archive) == before
    assert (root / "docs").is_dir()


def test_zip_creation_needs_an_existing_directory_parent(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    before = _names(zip_archive)
    with pytest.raises(FileNotFoundError):
        (root / "no" / "such").mkdir()
    with pytest.raises(FileNotFoundError):
        (root / "no" / "x.txt").write_text("x")
    with pytest.raises(NotADirectoryError):
        (root / "top.txt" / "x.txt").write_text("x")
    assert _names(zip_archive) == before
    (root / "no" / "such").mkdir(parents=True)
    (root / "no" / "such" / "x.txt").write_text("x")
    # An implicit zip directory (no "docs/" entry) is a valid parent.
    (root / "docs" / "new.txt").write_text("new")
    assert (root / "docs" / "new.txt").read_text() == "new"
    assert (root / "no" / "such" / "x.txt").read_text() == "x"


def test_zip_rename_returns_the_new_path(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    renamed = (root / "top.txt").rename("moved.txt")
    assert renamed.path == "moved.txt"
    assert renamed.read_text() == "top level"
    assert renamed.backend is root.backend


# --- ftparchive-rplus-writes-discarded (zip part) ---


def test_zip_r_plus_writes_reach_the_archive(zip_archive):
    member = UriPath(_zip_uri(zip_archive, "top.txt"))
    with member.open("r+b") as f:
        assert f.read(3) == b"top"
        f.seek(0)
        f.write(b"TOP")
    with zipfile.ZipFile(zip_archive) as zf:
        assert zf.read("top.txt") == b"TOP level"
        assert zf.namelist().count("top.txt") == 1
    with member.open("r+", encoding="utf-8") as f:
        f.seek(0, io.SEEK_END)
        f.write("!")
    assert member.read_text() == "TOP level!"


def test_zip_r_plus_without_writes_leaves_the_archive_untouched(zip_archive):
    before = zip_archive.read_bytes()
    with UriPath(_zip_uri(zip_archive, "top.txt")).open("r+b") as f:
        assert f.read() == b"top level"
    assert zip_archive.read_bytes() == before


def test_r_plus_on_a_read_only_archive_is_refused(zip_archive, tar_archive):
    import base64

    b64 = base64.b64encode(zip_archive.read_bytes()).decode()
    remote = UriPath(f"zip:data:application/zip;base64,{b64}!/top.txt")
    tar_member = UriPath(_tar_uri(tar_archive, "top.txt"))
    for member in (remote, tar_member):
        with pytest.raises(NotImplementedError):
            member.open("r+b")


# --- ftparchive-archive-ftp-mode-dropped (archive part) ---


def test_zip_member_reports_its_stored_unix_mode(tmp_path):
    path = tmp_path / "modes.zip"
    with zipfile.ZipFile(path, "w") as zf:
        tool = zipfile.ZipInfo("bin/tool", date_time=(2020, 1, 1, 0, 0, 0))
        tool.create_system = 3
        tool.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(tool, "#!/bin/sh\n")
        dos = zipfile.ZipInfo("dos.txt", date_time=(2020, 1, 1, 0, 0, 0))
        dos.create_system = 0
        dos.external_attr = 0x20
        zf.writestr(dos, "dos")
    root = UriPath(_zip_uri(path))
    st = (root / "bin" / "tool").stat()
    assert st.st_mode == stat.S_IFREG | 0o755
    assert st.mode_known
    placeholder = (root / "dos.txt").stat()
    assert not placeholder.mode_known
    assert stat.S_ISREG(placeholder.st_mode)


def test_tar_member_reports_its_stored_mode(tmp_path):
    path = tmp_path / "modes.tar"
    with tarfile.open(path, "w") as tf:
        info = tarfile.TarInfo("tool")
        info.size = 3
        info.mode = 0o750
        tf.addfile(info, io.BytesIO(b"abc"))
        folder = tarfile.TarInfo("dir")
        folder.type = tarfile.DIRTYPE
        folder.mode = 0o711
        tf.addfile(folder)
    root = UriPath(_tar_uri(path))
    st = (root / "tool").stat()
    assert (st.st_mode, st.mode_known) == (stat.S_IFREG | 0o750, True)
    st = (root / "dir").stat()
    assert (st.st_mode, st.mode_known) == (stat.S_IFDIR | 0o711, True)


def test_copying_a_member_out_keeps_it_writable_and_repeatable(tmp_path):
    path = tmp_path / "modes.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("tool", date_time=(2020, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(info, "#!/bin/sh\n")
    member = UriPath(_zip_uri(path, "tool"))
    out = LocalPath(tmp_path / "tool")
    member.copy(out)
    member.copy(out, overwrite=True)
    assert out.read_bytes() == b"#!/bin/sh\n"
    assert os.access(out, os.W_OK)
    if os.name != "nt":
        assert stat.S_IMODE(out.stat().st_mode) == 0o755


# --- memutils-archive-make-nonlocal-src / parity-make-archive-only-localpath-dirs ---


def _mem_tree():
    root = MemPath("/src")
    root.mkdir()
    (root / "top.txt").write_bytes(b"top level")
    (root / "docs").mkdir()
    (root / "docs" / "readme.txt").write_bytes(b"hello world")
    return root


@pytest.mark.parametrize("fmt", ["zip", "tar"])
@pytest.mark.parametrize("kind", ["mem", "fileuri"])
def test_make_archive_from_any_path_directory(tmp_path, fmt, kind):
    if kind == "mem":
        src = _mem_tree()
    else:
        local = tmp_path / "src"
        (local / "docs").mkdir(parents=True)
        (local / "top.txt").write_bytes(b"top level")
        (local / "docs" / "readme.txt").write_bytes(b"hello world")
        src = UriPath(local.as_uri())
    target = MemPath(f"/out.{fmt}")
    make_archive(src, fmt, target)
    dest = tmp_path / "out"
    unpack_archive(target, LocalPath(dest))
    assert _tree(dest) == _EXPECTED_TREE


@pytest.mark.parametrize("fmt", ["zip", "tar"])
def test_make_archive_failure_leaves_existing_target_untouched(
    tmp_path, monkeypatch, fmt
):
    target = tmp_path / f"out.{fmt}"
    target.write_bytes(b"precious")
    with pytest.raises(FileNotFoundError):
        make_archive(MemPath("/does-not-exist"), fmt, LocalPath(target))
    assert target.read_bytes() == b"precious"

    src = _mem_tree()
    original_open = MemPath._open

    def failing_open(self, mode="r", buffering=-1):
        if self.name == "readme.txt" and "r" in mode:
            raise PermissionError("unreadable")
        return original_open(self, mode, buffering)

    monkeypatch.setattr(MemPath, "_open", failing_open)
    with pytest.raises(PermissionError):
        make_archive(src, fmt, LocalPath(target))
    assert target.read_bytes() == b"precious"


# --- memutils-archive-zip64 ---


def test_make_archive_zip_member_past_the_zip64_limit(tmp_path, monkeypatch):
    # Stands in for a >2 GiB member: zipfile compares against ZIP64_LIMIT
    # at write time, so a small limit exercises the same branch.
    payload = os.urandom(20000)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "big.bin").write_bytes(payload)
    target = tmp_path / "big.zip"
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 4096)
    make_archive(LocalPath(tmp_path / "src"), "zip", LocalPath(target))
    monkeypatch.undo()
    with zipfile.ZipFile(target) as zf:
        assert zf.read("big.bin") == payload
        assert zf.testzip() is None


# --- memutils-archive-zip-nonseekable ---


class _NonSeekableArchive:
    """A `Path`-like archive whose stream cannot seek, like `HttpPath`."""

    def __init__(self, name, data):
        self.name = name
        self._data = data

    def open(self, mode="rb"):
        class _Raw(io.RawIOBase):
            def __init__(inner):
                inner._source = io.BytesIO(self._data)

            def readable(inner):
                return True

            def readinto(inner, buffer):
                chunk = inner._source.read(len(buffer))
                buffer[: len(chunk)] = chunk
                return len(chunk)

        return io.BufferedReader(_Raw())


@pytest.mark.parametrize("fmt", ["zip", "tar"])
def test_unpack_archive_from_a_non_seekable_stream(
    tmp_path, zip_archive, tar_archive, fmt
):
    source = zip_archive if fmt == "zip" else tar_archive
    archive = _NonSeekableArchive(source.name, source.read_bytes())
    assert not archive.open().seekable()
    dest = tmp_path / "out"
    unpack_archive(archive, LocalPath(dest))
    assert _tree(dest) == _EXPECTED_TREE


# --- memutils-archive-tar-links-dropped ---


def test_unpack_archive_extracts_tar_links_and_reports_the_rest(tmp_path):
    path = tmp_path / "links.tar"
    with tarfile.open(path, "w") as tf:
        orig = tarfile.TarInfo("orig.txt")
        orig.size = 5
        tf.addfile(orig, io.BytesIO(b"hello"))
        for name, kind, target in [
            ("hard.txt", tarfile.LNKTYPE, "orig.txt"),
            ("sub/sym.txt", tarfile.SYMTYPE, "../orig.txt"),
            ("escape.txt", tarfile.SYMTYPE, "../../outside.txt"),
        ]:
            link = tarfile.TarInfo(name)
            link.type = kind
            link.linkname = target
            tf.addfile(link)
    dest = MemPath("/dest")
    with pytest.warns(UserWarning, match="escape.txt"):
        unpack_archive(LocalPath(path), dest)
    assert (dest / "orig.txt").read_bytes() == b"hello"
    assert (dest / "hard.txt").read_bytes() == b"hello"
    assert (dest / "sub" / "sym.txt").read_bytes() == b"hello"
    assert not (dest / "escape.txt").exists()


def test_unpack_archive_without_links_emits_no_warning(tmp_path, tar_archive):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        unpack_archive(LocalPath(tar_archive), LocalPath(tmp_path / "out"))
    assert _tree(tmp_path / "out") == _EXPECTED_TREE
