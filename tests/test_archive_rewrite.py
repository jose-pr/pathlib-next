"""A zip mutation copies the entries it does not touch as they lie in the
archive: nothing is decompressed, the result is a valid archive, and its
members equal what decompressing and recompressing every entry gives (the
reference below is that algorithm, on the stdlib alone)."""

import io
import os
import shutil
import struct
import time
import zipfile
import zlib

import pytest

pytest.importorskip("uritools")

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.archive.zip import _ZipBackend


def _zip_uri(path, inner=""):
    return f"zip:{path.as_uri()}!/{inner}"


def _text(i, size=60):
    return (b"line %d of member text\n" % i) * size


MEMBERS = {
    "top.txt": _text(0),
    "d/one.txt": _text(1),
    "d/sub/two.txt": _text(2),
    "e/three.txt": _text(3),
    "e/": b"",
    "stored.bin": _text(4),
    "z.txt": _text(5),
}


def _write_members(target, compression_of=lambda name: zipfile.ZIP_DEFLATED):
    for index, (name, data) in enumerate(MEMBERS.items()):
        info = zipfile.ZipInfo(name, date_time=(2020, 1 + index, 2 + index, 3, 4, 5))
        info.compress_type = compression_of(name)
        info.external_attr = (0o644 << 16) | (0x10 if name.endswith("/") else 0)
        info.comment = b"c-%d" % index
        target.writestr(info, data)


def _plain(path):
    with zipfile.ZipFile(path, "w") as zf:
        _write_members(
            zf,
            lambda name: (
                zipfile.ZIP_STORED if name == "stored.bin" else zipfile.ZIP_DEFLATED
            ),
        )
        zf.comment = b"the archive comment"


def _with_stub(path):
    buffer = io.BytesIO(b'#!/bin/sh\nexec python3 "$0"\n')
    buffer.seek(0, 2)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_members(zf)
    path.write_bytes(buffer.getvalue())


class _Unseekable:
    """A sink `zipfile` cannot seek in: it writes data descriptors."""

    def __init__(self, target):
        self._target = target

    def write(self, data):
        return self._target.write(data)

    def flush(self):
        pass


def _streamed(path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(_Unseekable(buffer), "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in MEMBERS.items():
            with zf.open(zipfile.ZipInfo(name, (2020, 1, 2, 3, 4, 5)), "w") as out:
                out.write(data)
    path.write_bytes(buffer.getvalue())
    with zipfile.ZipFile(path) as zf:
        assert all(info.flag_bits & 0x08 for info in zf.infolist())


def _zip64_headers(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in MEMBERS.items():
            with zf.open(name, "w", force_zip64=True) as out:
                out.write(data)


def _many_methods(path):
    methods = [
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
    ]
    with zipfile.ZipFile(path, "w") as zf:
        _write_members(zf, lambda name: methods[len(name) % len(methods)])


def _legacy_names(path):
    # Names written in cp437 without the UTF-8 flag, as old tools do.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cafX.txt", _text(7))
        for name, data in MEMBERS.items():
            zf.writestr(name, data)
    path.write_bytes(path.read_bytes().replace(b"cafX.txt", b"caf\x82.txt"))


VARIANTS = {
    "plain": _plain,
    "stub": _with_stub,
    "streamed": _streamed,
    "zip64 headers": _zip64_headers,
    "other methods": _many_methods,
    "legacy names": _legacy_names,
}

OPERATIONS = {
    "drop a file": dict(exclude={"top.txt"}),
    "drop a directory": dict(exclude={"d/one.txt", "d/sub/two.txt"}),
    "drop a file and mark its directory": dict(
        exclude={"e/three.txt"}, overwrite={"e/": b""}
    ),
    "rename a file": dict(rename={"top.txt": "moved/top.txt"}),
    "rename a directory": dict(
        rename={"d/one.txt": "x/one.txt", "d/sub/two.txt": "x/sub/two.txt"}
    ),
    "overwrite a file": dict(overwrite={"top.txt": b"replaced"}),
    "rename over a file": dict(exclude={"z.txt"}, rename={"top.txt": "z.txt"}),
}


def _reference_rewrite(source, target, *, exclude=(), rename=None, overwrite=None):
    """Every entry decompressed and compressed again, with its metadata."""
    rename = dict(rename or {})
    overwrite = dict(overwrite or {})
    with (
        zipfile.ZipFile(source) as src,
        zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        dst.comment = src.comment
        plan = {}
        for info in src.infolist():
            if info.filename in exclude:
                continue
            plan[rename.get(info.filename, info.filename)] = info
        for name, info in plan.items():
            copied = zipfile.ZipInfo(name, date_time=info.date_time)
            copied.compress_type = info.compress_type
            copied.comment = info.comment
            copied.external_attr = info.external_attr
            data = overwrite.pop(name, None)
            if data is None:
                data = src.read(info)
            else:
                copied.date_time = time.localtime()[:6]
            dst.writestr(copied, data)
        for name, data in overwrite.items():
            dst.writestr(name, data)


def _state(path):
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        return {
            "comment": zf.comment,
            "members": [
                (
                    info.filename,
                    zf.read(info),
                    info.compress_type,
                    info.external_attr,
                    info.comment,
                )
                for info in zf.infolist()
            ],
            "dates": {i.filename: i.date_time for i in zf.infolist()},
        }


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("variant", VARIANTS)
def test_a_rewrite_leaves_the_members_the_decompressing_rewrite_leaves(
    tmp_path, variant, operation
):
    original = tmp_path / "original.zip"
    VARIANTS[variant](original)
    options = OPERATIONS[operation]
    expected = tmp_path / "expected.zip"
    _reference_rewrite(
        original,
        expected,
        exclude=options.get("exclude", ()),
        rename=options.get("rename"),
        overwrite=dict(options.get("overwrite") or {}),
    )
    actual = tmp_path / "actual.zip"
    shutil.copy(original, actual)
    backend = _ZipBackend(UriPath(actual.as_uri()))
    backend._rewrite(
        exclude=set(options.get("exclude", ())),
        rename=options.get("rename"),
        overwrite=dict(options.get("overwrite") or {}),
    )
    got, want = _state(actual), _state(expected)
    assert got["comment"] == want["comment"]
    assert got["members"] == want["members"]
    touched = set(options.get("overwrite") or ())
    assert {n: d for n, d in got["dates"].items() if n not in touched} == {
        n: d for n, d in want["dates"].items() if n not in touched
    }


def test_the_bytes_before_the_first_member_and_the_comment_survive(tmp_path):
    archive = tmp_path / "stub.zip"
    _with_stub(archive)
    prefix = archive.read_bytes()[:22]
    (UriPath(_zip_uri(archive)) / "top.txt").unlink()
    assert archive.read_bytes()[:22] == prefix
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
        assert "top.txt" not in zf.namelist()


# --- nothing is decompressed ----------------------------------------------------------------


class _InflateCount:
    def __init__(self, monkeypatch):
        self.bytes = 0
        self.calls = 0
        real = zlib.decompressobj
        counter = self

        class Counting:
            def __init__(self, *args):
                self._real = real(*args)

            def decompress(self, data, *args):
                out = self._real.decompress(data, *args)
                counter.bytes += len(out)
                counter.calls += 1
                return out

            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(zlib, "decompressobj", lambda *args: Counting(*args))


def test_unlinking_one_member_inflates_nothing(tmp_path, monkeypatch):
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(100):
            zf.writestr(f"m{i:03d}.txt", _text(i, 500))
    count = _InflateCount(monkeypatch)
    (UriPath(_zip_uri(archive)) / "m000.txt").unlink()
    assert (count.bytes, count.calls) == (0, 0)
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
        assert len(zf.namelist()) == 99


def test_removing_a_tree_and_renaming_one_inflate_nothing(tmp_path, monkeypatch):
    archive = tmp_path / "tree.zip"
    _plain(archive)
    count = _InflateCount(monkeypatch)
    root = UriPath(_zip_uri(archive))
    (root / "d").rm(recursive=True)
    (root / "e" / "three.txt").rename("four.txt")
    assert (count.bytes, count.calls) == (0, 0)
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
        assert "d/one.txt" not in zf.namelist() and "e/four.txt" in zf.namelist()


# --- an entry that cannot be read does not stop a change to another ------------------------


def _encrypt_flag(path, name):
    raw = bytearray(path.read_bytes())
    for signature, flags_at, name_at in (
        (b"PK\x03\x04", 6, 30),
        (b"PK\x01\x02", 8, 46),
    ):
        at = 0
        while (at := raw.find(signature, at)) >= 0:
            length_at = at + (26 if signature[2] == 3 else 28)
            (length,) = struct.unpack_from("<H", raw, length_at)
            if bytes(raw[at + name_at : at + name_at + length]) == name.encode():
                raw[at + flags_at] |= 1
            at += 4
    path.write_bytes(bytes(raw))


def test_an_encrypted_member_survives_changes_to_other_members(tmp_path):
    archive = tmp_path / "enc.zip"
    _plain(archive)
    with zipfile.ZipFile(archive) as zf:
        sealed = zf.getinfo("d/one.txt")
        before = (sealed.compress_size, sealed.CRC)
        start = sealed.header_offset
    _encrypt_flag(archive, "d/one.txt")
    root = UriPath(_zip_uri(archive))
    (root / "top.txt").unlink()
    (root / "z.txt").write_bytes(b"over")
    (root / "e" / "three.txt").rename("four.txt")
    with zipfile.ZipFile(archive) as zf:
        info = zf.getinfo("d/one.txt")
        assert info.flag_bits & 1
        assert (info.compress_size, info.CRC) == before
        assert zf.read("z.txt") == b"over"
        assert zf.read("e/four.txt") == _text(3)
        assert "top.txt" not in zf.namelist()
    with pytest.raises(RuntimeError):
        (root / "d" / "one.txt").read_bytes()
    assert start >= 0


def test_removing_forty_members_one_by_one_is_one_rewrite_each_and_inflates_nothing(
    tmp_path, monkeypatch
):
    archive = tmp_path / "forty.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(120):
            zf.writestr(f"m{i:03d}.txt", _text(i, 200))
    rewrites = []
    real = _ZipBackend._replace_outer

    def counted(self, fill):
        rewrites.append(1)
        return real(self, fill)

    monkeypatch.setattr(_ZipBackend, "_replace_outer", counted)
    count = _InflateCount(monkeypatch)
    root = UriPath(_zip_uri(archive))
    for i in range(40):
        (root / f"m{i:03d}.txt").unlink()
    assert len(rewrites) == 40
    assert (count.bytes, count.calls) == (0, 0)
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == [f"m{i:03d}.txt" for i in range(40, 120)]
