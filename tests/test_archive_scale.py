"""What listing, walking and reading an archive cost as the archive grows:
counts and bounds, not timings. A walk compares each name a bounded number of
times, and reading a small member holds a small amount of memory however
large the archive or the member is."""

import io
import sys
import tarfile
import tracemalloc
import zipfile

import pytest

pytest.importorskip("uritools")

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.archive import _base


def _zip_uri(path, inner=""):
    return f"zip:{path.as_uri()}!/{inner}"


def _tar_uri(path, inner=""):
    return f"tar:{path.as_uri()}!/{inner}"


class _Count:
    """Counts the calls of one built-in method (`str.startswith`) made while
    it is active, in the calling thread."""

    def __init__(self, name):
        self.name = name
        self.calls = 0

    def _profile(self, frame, event, arg):
        if event == "c_call" and getattr(arg, "__name__", None) == self.name:
            self.calls += 1

    def __enter__(self):
        sys.setprofile(self._profile)
        return self

    def __exit__(self, *exc):
        sys.setprofile(None)


def _many_directories(path, directories, kind):
    names = [f"pkg/d{i:05d}/f{j}.txt" for i in range(directories) for j in range(4)]
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as zf:
            for name in names:
                zf.writestr(name, b"x")
    else:
        with tarfile.open(path, "w") as tf:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = 1
                tf.addfile(info, io.BytesIO(b"x"))
    return len(names)


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_a_walk_compares_each_name_a_bounded_number_of_times(tmp_path, kind):
    for directories in (50, 100, 200):
        path = tmp_path / f"{directories}.{kind}"
        members = _many_directories(path, directories, kind)
        root = UriPath(f"{kind}:{path.as_uri()}!/")
        with _Count("startswith") as compared:
            files = sum(len(names) for _, _, names in root.walk())
        assert files == members
        assert compared.calls <= 2 * members, (directories, compared.calls)


def test_listing_a_directory_of_a_large_archive_reads_only_its_children(tmp_path):
    path = tmp_path / "wide.zip"
    members = _many_directories(path, 400, "zip")
    root = UriPath(_zip_uri(path))
    assert [p.name for p in (root / "pkg" / "d00123").iterdir()] == [
        "f0.txt",
        "f1.txt",
        "f2.txt",
        "f3.txt",
    ]
    with _Count("startswith") as compared:
        assert len(list((root / "pkg" / "d00300").iterdir())) == 4
        assert (root / "pkg" / "d00300" / "f1.txt").exists()
    assert compared.calls < members // 10


# --- memory ----------------------------------------------------------------------


class _Zeros:
    def __init__(self, size):
        self.left = size

    def read(self, size=-1):
        size = self.left if size is None or size < 0 else min(size, self.left)
        self.left -= size
        return bytes(size)


def _peak(action):
    tracemalloc.start()
    try:
        result = action()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


def test_reading_a_small_member_of_a_large_local_tar_holds_under_a_mebibyte(tmp_path):
    archive = tmp_path / "big.tar"
    with tarfile.open(archive, "w") as tf:
        pad = tarfile.TarInfo("pad")
        pad.size = 32 * 2**20
        tf.addfile(pad, _Zeros(pad.size))
        small = tarfile.TarInfo("small")
        small.size = 5
        tf.addfile(small, io.BytesIO(b"hello"))
    root = UriPath(_tar_uri(archive))
    data, peak = _peak(lambda: (root / "small").read_bytes())
    assert data == b"hello"
    assert peak < 2**20


def test_reading_a_small_member_of_a_large_compressed_tar_holds_under_a_mebibyte(
    tmp_path,
):
    archive = tmp_path / "big.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        pad = tarfile.TarInfo("pad")
        pad.size = 12 * 2**20
        tf.addfile(pad, _Zeros(pad.size))
        small = tarfile.TarInfo("small")
        small.size = 5
        tf.addfile(small, io.BytesIO(b"hello"))
    root = UriPath(_tar_uri(archive))
    data, peak = _peak(lambda: (root / "small").read_bytes())
    assert data == b"hello"
    assert peak < 2**20


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_a_member_past_the_spool_size_is_read_through_a_temporary_file(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(_base, "MEMBER_SPOOL_BYTES", 64 * 1024)
    size = 20 * 2**20
    if kind == "zip":
        archive = tmp_path / "bomb.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            with zf.open("zeros", "w") as out:
                out.write(bytes(size))
    else:
        archive = tmp_path / "bomb.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            info = tarfile.TarInfo("zeros")
            info.size = size
            tf.addfile(info, _Zeros(size))
    member = UriPath(f"{kind}:{archive.as_uri()}!/zeros")

    def read():
        with member.open("rb") as stream:
            first = stream.read(1)
            stream.seek(0, 2)
            return first, stream.tell(), type(stream)

    (first, length, kind_of_stream), peak = _peak(read)
    assert (first, length) == (b"\0", size)
    assert kind_of_stream is not io.BytesIO
    assert peak < 2**20 * 2


def test_a_member_under_the_spool_size_is_read_from_memory(tmp_path):
    archive = tmp_path / "small.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("m", b"x" * 1000)
    with (UriPath(_zip_uri(archive)) / "m").open("rb") as stream:
        assert isinstance(stream, io.BytesIO)
        assert stream.read() == b"x" * 1000


def test_a_spooled_member_stays_readable_after_the_archive_is_rewritten(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(_base, "MEMBER_SPOOL_BYTES", 1024)
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big", b"abc" * 100_000)
        zf.writestr("other", b"o")
    root = UriPath(_zip_uri(archive))
    with (root / "big").open("rb") as stream:
        (root / "other").unlink()
        (root / "new").write_bytes(b"n")
        assert stream.read() == b"abc" * 100_000
    assert (root / "new").read_bytes() == b"n"
