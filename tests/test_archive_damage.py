"""What a damaged archive, a concurrent rewrite and an unwritable archive look
like through the archive schemes: one exception family per format, a member
that vanishes mid-lookup is not found, and a refused write changes nothing."""

import gzip
import io
import os
import struct
import tarfile
import threading
import zipfile

import pytest

pytest.importorskip("uritools")

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.archive.zip import _ZipBackend


def _zip_uri(path, inner=""):
    return f"zip:{path.as_uri()}!/{inner}"


def _tar_uri(path, inner=""):
    return f"tar:{path.as_uri()}!/{inner}"


def _make_zip(path, members, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _make_tar(path, members, mode="w"):
    with tarfile.open(path, mode) as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


# --- a damaged zip is a BadZipFile, whatever decoder noticed ----------------------


def _corrupt_deflate_stream(path):
    raw = bytearray(path.read_bytes())
    start = raw.index(b"PK\x03\x04")
    name_len, extra_len = struct.unpack_from("<HH", raw, start + 26)
    data = start + 30 + name_len + extra_len
    raw[data : data + 8] = b"\xff" * 8
    path.write_bytes(bytes(raw))


def _declare_newer_zip_version(path):
    raw = bytearray(path.read_bytes())
    central = raw.index(b"PK\x01\x02")
    struct.pack_into("<H", raw, central + 6, 999)
    path.write_bytes(bytes(raw))


def test_a_member_whose_deflate_stream_is_damaged_reads_as_bad_zip_file(tmp_path):
    archive = _make_zip(tmp_path / "a.zip", {"m": b"hello world " * 400})
    _corrupt_deflate_stream(archive)
    root = UriPath(_zip_uri(archive))
    with pytest.raises(zipfile.BadZipFile):
        (root / "m").read_bytes()
    assert (root / "m").stat().st_size == len(b"hello world " * 400)


def test_a_central_directory_declaring_an_unknown_version_is_a_bad_zip_file(tmp_path):
    archive = _make_zip(tmp_path / "a.zip", {"m": b"data"})
    _declare_newer_zip_version(archive)
    root = UriPath(_zip_uri(archive))
    with pytest.raises(zipfile.BadZipFile):
        root.exists()
    with pytest.raises(zipfile.BadZipFile):
        (root / "m").stat()
    with pytest.raises(zipfile.BadZipFile):
        list(root.iterdir())


def test_an_unsupported_compression_method_is_still_not_implemented(tmp_path):
    archive = _make_zip(tmp_path / "a.zip", {"ok": b"fine", "odd": b"x" * 50})
    raw = bytearray(archive.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        at = 0
        while (at := raw.find(signature, at)) >= 0:
            header = 30 if signature[2] == 3 else 46
            length_at = at + (26 if signature[2] == 3 else 28)
            (length,) = struct.unpack_from("<H", raw, length_at)
            if bytes(raw[at + header : at + header + length]) == b"odd":
                struct.pack_into("<H", raw, at + offset, 99)
            at += 4
    archive.write_bytes(bytes(raw))
    root = UriPath(_zip_uri(archive))
    assert (root / "ok").read_bytes() == b"fine"
    with pytest.raises(NotImplementedError):
        (root / "odd").read_bytes()


# --- a damaged tar is a ReadError ---------------------------------------------------------


def _damaged_tars(tmp_path):
    payload = {f"m{i}": os.urandom(4096) for i in range(8)}
    gz = _make_tar(tmp_path / "cut.tar.gz", payload, "w:gz")
    gz.write_bytes(gz.read_bytes()[: gz.stat().st_size * 2 // 3])
    xz = _make_tar(tmp_path / "cut.tar.xz", payload, "w:xz")
    xz.write_bytes(xz.read_bytes()[: xz.stat().st_size * 2 // 3])
    # Two gzip members; the first one's CRC-32 is wrong, and tarfile has to
    # read across the boundary to reach the second header.
    plain = _make_tar(tmp_path / "plain.tar", payload).read_bytes()
    first = bytearray(gzip.compress(plain[:1024]))
    first[-8] ^= 0xFF
    crc = tmp_path / "crc.tar.gz"
    crc.write_bytes(bytes(first) + gzip.compress(plain[1024:]))
    return {"cut gz": gz, "cut xz": xz, "gzip member CRC": crc}


@pytest.mark.parametrize("which", ["cut gz", "cut xz", "gzip member CRC"])
def test_a_damaged_compressed_tar_is_a_read_error_for_every_lookup(tmp_path, which):
    archive = _damaged_tars(tmp_path)[which]
    root = UriPath(_tar_uri(archive))
    for call in (
        root.exists,
        lambda: list(root.iterdir()),
        lambda: (root / "m0").exists(),
        lambda: (root / "m0").stat(),
        lambda: (root / "m0").read_bytes(),
    ):
        with pytest.raises(tarfile.ReadError):
            call()


# --- a member that vanishes between the lookup and its stat ------------------------------


def test_a_member_dropped_between_the_lookup_and_its_stat_is_not_found(
    tmp_path, monkeypatch
):
    archive = _make_zip(tmp_path / "a.zip", {"keep": b"k", "flip": b"f"})
    flip = UriPath(_zip_uri(archive, "flip"))
    assert flip.exists()
    real = _ZipBackend.member_stat
    rewritten = []

    def stat_after_another_writer_dropped_it(self, path):
        if not rewritten:
            rewritten.append(1)
            _make_zip(archive, {"keep": b"k", "pad": b"p" * 100})
        return real(self, path)

    monkeypatch.setattr(
        _ZipBackend, "member_stat", stat_after_another_writer_dropped_it
    )
    assert rewritten == []
    assert not flip.exists()
    assert rewritten == [1]
    with pytest.raises(FileNotFoundError):
        flip.stat()


def test_readers_never_see_anything_but_an_oserror_while_another_thread_rewrites(
    tmp_path,
):
    archive = _make_zip(tmp_path / "rw.zip", {"keep": b"k", "flip": b"f"})
    stop = threading.Event()
    escaped = []

    def reader():
        flip = UriPath(_zip_uri(archive, "flip"))
        keep = UriPath(_zip_uri(archive, "keep"))
        while not stop.is_set():
            for call in (flip.exists, flip.is_file, keep.read_bytes, keep.stat):
                try:
                    call()
                except OSError:
                    pass
                except BaseException as error:  # noqa: BLE001
                    escaped.append(repr(error))

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for thread in readers:
        thread.start()
    writer = UriPath(_zip_uri(archive, "flip"))
    try:
        for i in range(40):
            try:
                writer.unlink(missing_ok=True)
                writer.write_bytes(b"f%d" % i)
            except OSError:
                pass  # Windows refuses a replace while a reader holds the file
    finally:
        stop.set()
        for thread in readers:
            thread.join()
    assert escaped == []


# --- an archive that may not be written is refused, and left as it was -------------------


def _is_superuser():
    return hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.mark.skipif(_is_superuser(), reason="the superuser may write a mode-0444 file")
def test_mutating_a_read_only_zip_is_refused_and_leaves_it_untouched(tmp_path):
    archive = _make_zip(tmp_path / "ro.zip", {"a": b"A", "b": b"B"})
    before = archive.read_bytes()
    os.chmod(archive, 0o444)
    root = UriPath(_zip_uri(archive))
    try:
        for call in (
            lambda: (root / "new").write_bytes(b"N"),
            lambda: (root / "a").write_bytes(b"over"),
            lambda: (root / "a").unlink(),
            lambda: (root / "a").rename("c"),
            lambda: (root / "d").mkdir(),
        ):
            with pytest.raises(PermissionError):
                call()
        assert archive.read_bytes() == before
        assert [p.name for p in tmp_path.iterdir()] == ["ro.zip"]
        assert (root / "a").read_bytes() == b"A"
    finally:
        os.chmod(archive, 0o644)


def test_a_write_replaces_the_archive_so_another_hard_link_keeps_the_old_one(
    tmp_path,
):
    archive = _make_zip(tmp_path / "h.zip", {"a": b"A"})
    link = tmp_path / "h-link.zip"
    try:
        os.link(archive, link)
    except (OSError, AttributeError, NotImplementedError):
        pytest.skip("the filesystem has no hard links")
    (UriPath(_zip_uri(archive)) / "new").write_bytes(b"n")
    with zipfile.ZipFile(archive) as zf:
        assert sorted(zf.namelist()) == ["a", "new"]
    with zipfile.ZipFile(link) as zf:
        assert zf.namelist() == ["a"]


# --- members written through zip: are compressed ----------------------------------------


def test_a_new_member_is_deflated_and_an_overwritten_member_keeps_its_method(tmp_path):
    archive = tmp_path / "c.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("mimetype", b"application/x-first", zipfile.ZIP_STORED)
        zf.writestr("packed", b"p" * 50_000, zipfile.ZIP_DEFLATED)
    root = UriPath(_zip_uri(archive))
    (root / "added").write_bytes(b"A" * 100_000)
    (root / "mimetype").write_bytes(b"application/x-second")
    (root / "packed").write_bytes(b"q" * 50_000)
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
        kinds = {i.filename: (i.compress_type, i.compress_size) for i in zf.infolist()}
        assert zf.read("mimetype") == b"application/x-second"
    assert kinds["added"][0] == zipfile.ZIP_DEFLATED and kinds["added"][1] < 1000
    assert kinds["mimetype"][0] == zipfile.ZIP_STORED
    assert kinds["packed"][0] == zipfile.ZIP_DEFLATED


def test_the_first_member_of_a_new_archive_is_deflated(tmp_path):
    archive = tmp_path / "fresh.zip"
    (UriPath(_zip_uri(archive)) / "a.txt").write_bytes(b"A" * 100_000)
    with zipfile.ZipFile(archive) as zf:
        (info,) = zf.infolist()
        assert zf.testzip() is None
    assert info.compress_type == zipfile.ZIP_DEFLATED
    assert info.compress_size < 1000
