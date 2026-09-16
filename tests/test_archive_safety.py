"""Archive data-loss and traversal regressions (2026-09-15 deep review).

Each test asserts what must SURVIVE: the bytes of an archive that was only
read, member metadata across a rewrite, one entry per name after a rename,
and nothing written outside an extraction/copy destination.
"""

import errno
import gc
import os
import stat
import struct
import tarfile
import io
import warnings
import zipfile

import pytest

from pathlib_next import LocalPath
from pathlib_next import path as path_module
from pathlib_next.mempath import MemPath
from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.archive.zip import _strip_zip64_extra
from pathlib_next.utils import unpack_archive


def _zip_uri(path, inner=""):
    return f"zip:{path.as_uri()}!/{inner}"


def _tar_uri(path, inner=""):
    return f"tar:{path.as_uri()}!/{inner}"


def _release():
    # Backends are shared through a weak registry and close their handle in
    # `__del__`: collect so no handle outlives the objects the test dropped.
    gc.collect()


@pytest.fixture
def zip_archive(tmp_path):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("docs/readme.txt", "hello world")
        zf.writestr("top.txt", "top level")
    return path


def _write_zip(path, members):
    """`members`: (name, data) pairs; names are set raw on the ZipInfo so
    crafted names (backslashes, drives) reach the archive unchanged."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate-name warnings
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in members:
                info = zipfile.ZipInfo("placeholder", date_time=(2001, 2, 3, 4, 5, 6))
                info.filename = name
                zf.writestr(info, data)
    return path


def _write_tar(path, members):
    with tarfile.open(path, "w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


# --- ftparchive-zip-read-opens-append-mode ---


def test_zip_read_only_archive_is_readable_and_unmodified(zip_archive):
    before = zip_archive.read_bytes()
    os.chmod(zip_archive, stat.S_IREAD)
    try:
        readme = UriPath(_zip_uri(zip_archive, "docs/readme.txt"))
        assert readme.exists()
        assert readme.read_text() == "hello world"
        assert [p.name for p in readme.parent.iterdir()] == ["readme.txt"]
        del readme
        _release()
        assert zip_archive.read_bytes() == before
    finally:
        os.chmod(zip_archive, stat.S_IREAD | stat.S_IWRITE)


def test_zip_exists_on_missing_archive_does_not_create_it(tmp_path):
    missing = tmp_path / "missing.zip"
    member = UriPath(_zip_uri(missing, "x.txt"))
    assert not member.exists()
    with pytest.raises(FileNotFoundError):
        member.read_bytes()
    del member
    _release()
    assert not missing.exists()


def test_zip_first_write_creates_missing_archive(tmp_path):
    created = tmp_path / "new.zip"
    root = UriPath(_zip_uri(created))
    (root / "x.txt").write_text("created")
    (root / "y.txt").write_text("appended")
    assert (root / "x.txt").read_text() == "created"
    with zipfile.ZipFile(created) as zf:
        assert zf.namelist() == ["x.txt", "y.txt"]
        assert zf.read("y.txt") == b"appended"


def test_zip_probe_of_non_zip_file_leaves_it_unmodified(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"not a zip file")
    member = UriPath(_zip_uri(notes, "x.txt"))
    with pytest.raises(zipfile.BadZipFile):
        member.exists()
    with pytest.raises(zipfile.BadZipFile):
        member.write_text("x")
    del member
    _release()
    assert notes.read_bytes() == b"not a zip file"


def test_zip_append_keeps_existing_entries_and_prefix(tmp_path):
    path = tmp_path / "app.zip"
    with path.open("wb") as raw:
        raw.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(raw, "w") as zf:
            zf.writestr("__main__.py", "print('hi')")
    (UriPath(_zip_uri(path)) / "extra.txt").write_text("x")
    assert path.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist() == ["__main__.py", "extra.txt"]


# --- ftparchive-zip-rewrite-drops-metadata ---

_SHEBANG = b"#!/usr/bin/env python3\n"
_EXTRA = struct.pack("<HH", 0xCAFE, 2) + b"hi"


@pytest.fixture
def rich_zip(tmp_path):
    path = tmp_path / "rich.zip"
    with path.open("wb") as raw:
        raw.write(_SHEBANG)
        with zipfile.ZipFile(raw, "w") as zf:
            zf.comment = b"archive comment"
            mimetype = zipfile.ZipInfo("mimetype", date_time=(2001, 2, 3, 4, 5, 6))
            mimetype.compress_type = zipfile.ZIP_STORED
            zf.writestr(mimetype, b"application/epub+zip")

            bindir = zipfile.ZipInfo("bin/", date_time=(2003, 3, 3, 3, 3, 4))
            bindir.external_attr = (0o40755 << 16) | 0x10
            bindir.create_system = 3
            zf.writestr(bindir, b"")

            script = zipfile.ZipInfo("bin/run.sh", date_time=(2002, 1, 1, 0, 0, 0))
            script.compress_type = zipfile.ZIP_DEFLATED
            script.external_attr = 0o100755 << 16
            script.create_system = 3
            script.comment = b"member comment"
            script.extra = _EXTRA
            zf.writestr(script, b"#!/bin/sh\necho hi\n" * 10)

            junk = zipfile.ZipInfo("junk.txt", date_time=(2004, 4, 4, 4, 4, 4))
            junk.compress_type = zipfile.ZIP_STORED
            junk.external_attr = 0o100644 << 16
            zf.writestr(junk, b"junk")
    return path


def _snapshot(path):
    with zipfile.ZipFile(path) as zf:
        members = {
            info.filename: (
                info.date_time,
                info.compress_type,
                info.external_attr,
                info.create_system,
                info.extra,
                info.comment,
                zf.read(info),
            )
            for info in zf.infolist()
        }
        return zf.comment, members


@pytest.mark.parametrize(
    "mutate, removed",
    [
        (lambda root: (root / "junk.txt").unlink(), "junk.txt"),
        (lambda root: (root / "junk.txt").rename("moved.txt"), "junk.txt"),
        (lambda root: (root / "junk.txt").write_bytes(b"replaced"), None),
    ],
    ids=["unlink", "rename", "overwrite"],
)
def test_zip_rewrite_keeps_other_members_metadata(rich_zip, mutate, removed):
    comment, before = _snapshot(rich_zip)
    mutate(UriPath(_zip_uri(rich_zip)))
    _release()

    assert rich_zip.read_bytes().startswith(_SHEBANG)
    after_comment, after = _snapshot(rich_zip)
    assert after_comment == comment == b"archive comment"
    for name in ("mimetype", "bin/", "bin/run.sh"):
        assert after[name] == before[name], name
    assert after["bin/run.sh"][4] == _EXTRA
    assert after["mimetype"][1] == zipfile.ZIP_STORED
    if removed:
        assert removed not in after
    if removed and "moved.txt" in after:
        assert after["moved.txt"] == before["junk.txt"]


def test_zip_overwrite_keeps_member_compression_and_mode(rich_zip):
    _, before = _snapshot(rich_zip)
    (UriPath(_zip_uri(rich_zip)) / "junk.txt").write_bytes(b"replaced")
    _, after = _snapshot(rich_zip)
    assert after["junk.txt"][6] == b"replaced"
    assert after["junk.txt"][1:3] == before["junk.txt"][1:3]


def test_strip_zip64_extra_keeps_other_records():
    zip64 = struct.pack("<HHQ", 0x0001, 8, 123)
    assert _strip_zip64_extra(zip64 + _EXTRA + zip64) == _EXTRA
    assert _strip_zip64_extra(b"") == b""


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_zip_rewrite_keeps_archive_file_mode(zip_archive):
    os.chmod(zip_archive, 0o644)
    (UriPath(_zip_uri(zip_archive)) / "top.txt").unlink()
    assert stat.S_IMODE(os.stat(zip_archive).st_mode) == 0o644


def test_zip_rewrite_through_symlink_replaces_its_target(tmp_path, zip_archive):
    link = tmp_path / "link.zip"
    try:
        link.symlink_to(zip_archive)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks unavailable: {error}")
    (UriPath(_zip_uri(link)) / "top.txt").unlink()
    _release()
    assert link.is_symlink()
    with zipfile.ZipFile(zip_archive) as zf:
        assert zf.namelist() == ["docs/readme.txt"]


@pytest.mark.skipif(
    os.name != "nt", reason="Windows refuses to replace a read-only file"
)
def test_zip_failed_rewrite_leaves_archive_and_no_temp_file(zip_archive):
    before = zip_archive.read_bytes()
    os.chmod(zip_archive, stat.S_IREAD)
    try:
        with pytest.raises(PermissionError):
            (UriPath(_zip_uri(zip_archive)) / "top.txt").unlink()
        _release()
        assert zip_archive.read_bytes() == before
        assert [p.name for p in zip_archive.parent.iterdir()] == ["a.zip"]
    finally:
        os.chmod(zip_archive, stat.S_IREAD | stat.S_IWRITE)


# --- ftparchive-zip-rename-onto-existing ---


@pytest.fixture
def abc_zip(tmp_path):
    return _write_zip(
        tmp_path / "abc.zip",
        [
            ("a.txt", b"NEW CONTENT FROM a"),
            ("b.txt", b"old b"),
            ("c.txt", b"c"),
            ("src/", b""),
            ("src/x.txt", b"x"),
            ("empty/", b""),
            ("full/y.txt", b"y"),
        ],
    )


def _names(path):
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def test_zip_rename_onto_existing_file_replaces_it(abc_zip):
    root = UriPath(_zip_uri(abc_zip))
    (root / "a.txt").rename("b.txt")
    names = _names(abc_zip)
    assert names.count("b.txt") == 1
    assert "a.txt" not in names
    assert (root / "b.txt").read_bytes() == b"NEW CONTENT FROM a"
    # A later, unrelated rewrite must not resurrect the replaced content.
    (root / "c.txt").unlink()
    with zipfile.ZipFile(abc_zip) as zf:
        assert zf.namelist().count("b.txt") == 1
        assert zf.read("b.txt") == b"NEW CONTENT FROM a"


def test_zip_rename_directory_onto_empty_directory_replaces_it(abc_zip):
    root = UriPath(_zip_uri(abc_zip))
    (root / "src").rename("empty")
    names = _names(abc_zip)
    assert names.count("empty/") == 1
    assert "empty/x.txt" in names
    assert not any(n.startswith("src/") for n in names)


@pytest.mark.parametrize(
    "source, target, error, code",
    [
        ("src", "full", OSError, errno.ENOTEMPTY),
        ("a.txt", "full", IsADirectoryError, errno.EISDIR),
        ("src", "b.txt", NotADirectoryError, errno.ENOTDIR),
    ],
)
def test_zip_rename_refusals_leave_archive_unchanged(
    abc_zip, source, target, error, code
):
    before = abc_zip.read_bytes()
    root = UriPath(_zip_uri(abc_zip))
    with pytest.raises(error) as raised:
        (root / source).rename(target)
    assert raised.value.errno == code
    assert abc_zip.read_bytes() == before


def test_zip_rewrite_collapses_existing_duplicates_to_the_live_entry(tmp_path):
    path = _write_zip(
        tmp_path / "dup.zip",
        [("dup.txt", b"stale"), ("other.txt", b"o"), ("dup.txt", b"live")],
    )
    root = UriPath(_zip_uri(path))
    (root / "other.txt").unlink()
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist() == ["dup.txt"]
        assert zf.read("dup.txt") == b"live"


# --- ftparchive-archive-member-path-traversal ---

_CRAFTED_MEMBERS = [
    ("pkg/ok.txt", b"ok"),
    ("pkg/../../ESCAPED_DOTDOT.txt", b"evil"),
    ("pkg/..\\..\\ESCAPED_BACKSLASH.txt", b"evil"),
    ("pkg/C:../C:../ESCAPED_DRIVE_RELATIVE.txt", b"evil"),
    ("pkg/D:evil.txt", b"evil"),
    ("/abs.txt", b"evil"),
]


def _crafted(tmp_path, fmt):
    tmp_path.mkdir(parents=True, exist_ok=True)
    if fmt == "zip":
        return _write_zip(tmp_path / "crafted.zip", _CRAFTED_MEMBERS)
    return _write_tar(tmp_path / "crafted.tar", _CRAFTED_MEMBERS)


_URI = {"zip": _zip_uri, "tar": _tar_uri}


def _crafted_pkg_children(fmt):
    """The members of `pkg` that survive, sorted. The escaping ones never
    appear; the drive-shaped ones do. They differ by format only because
    `zipfile` rewrites `\\` to `/` as it stores a name, which turns the
    backslash member into `pkg/../../ESCAPED_BACKSLASH.txt` -- an escape,
    dropped -- while tar keeps it as one ordinary filename."""
    children = ["C:..", "D:evil.txt", "ok.txt"]
    if fmt == "tar":
        children.append("..\\..\\ESCAPED_BACKSLASH.txt")
    return sorted(children)


@pytest.mark.parametrize("fmt", ["zip", "tar"])
def test_archive_listing_skips_escaping_member_names(tmp_path, fmt):
    """Only a name with no place inside the archive is skipped. A name that
    merely a *Windows destination* would misread (`D:evil.txt`, `C:..`,
    `..\\..\\x`) is an ordinary POSIX filename and stays listed; refusing to
    join it is the destination's job."""
    archive = _crafted(tmp_path, fmt)
    pkg = UriPath(_URI[fmt](archive, "pkg"))
    assert sorted(p.name for p in pkg.iterdir()) == _crafted_pkg_children(fmt)
    # "pkg/../../ESCAPED_DOTDOT.txt" and "/abs.txt" have no name in here.
    assert [p.name for p in UriPath(_URI[fmt](archive)).iterdir()] == ["pkg"]


@pytest.mark.parametrize("fmt", ["zip", "tar"])
def test_archive_recursive_copy_stays_inside_destination(tmp_path, fmt, monkeypatch):
    """A Windows destination refuses every child name it would read as a
    drive or a separator, so nothing lands outside it. Checked with the
    target's flavour forced, since the same archive is safe on POSIX."""
    monkeypatch.setattr(path_module._utils, "is_windows_flavoured", lambda p: True)
    archive = _crafted(tmp_path / "src", fmt)
    work = tmp_path / "work"
    dest = work / "deep" / "dest"
    dest.parent.mkdir(parents=True)
    pkg = UriPath(_URI[fmt](archive, "pkg"))
    refused = []
    pkg.copy(
        LocalPath(dest),
        recursive=True,
        overwrite=True,
        ignore_error=refused.append,
    )
    assert (dest / "ok.txt").read_bytes() == b"ok"
    assert [p.name for p in dest.iterdir()] == ["ok.txt"]
    expected = [n for n in _crafted_pkg_children(fmt) if n != "ok.txt"]
    messages = [str(e) for e in refused]
    assert len(messages) == len(expected)
    for name in expected:
        assert any(repr(name) in message for message in messages), name
    outside = [p for p in work.rglob("*") if p != dest and dest not in p.parents]
    assert outside == [dest.parent]


@pytest.mark.parametrize("fmt", ["zip", "tar"])
def test_archive_recursive_copy_keeps_posix_names_on_a_posix_destination(tmp_path, fmt):
    """The mirror image: to a destination that reads names with POSIX rules
    the very same members are ordinary files and are copied. `MemPath` is
    that destination on any host -- Windows itself cannot hold these names,
    so a local directory could not show this on this machine."""
    archive = _crafted(tmp_path / "src", fmt)
    dest = MemPath("/dest")
    dest.mkdir(parents=True)
    pkg = UriPath(_URI[fmt](archive, "pkg"))
    pkg.copy(dest, recursive=True, overwrite=True)
    assert sorted(p.name for p in dest.iterdir()) == _crafted_pkg_children(fmt)
    assert (dest / "ok.txt").read_bytes() == b"ok"


@pytest.mark.parametrize("fmt", ["zip", "tar"])
def test_archive_safe_names_with_colons_are_still_listed_and_readable(tmp_path, fmt):
    members = [("pkg/12:00.log", b"noon")]
    write = _write_zip if fmt == "zip" else _write_tar
    archive = write(tmp_path / f"colon.{fmt}", members)
    (child,) = UriPath(_URI[fmt](archive, "pkg")).iterdir()
    assert child.name == "12:00.log"
    assert child.read_bytes() == b"noon"


# --- member names are normalized POSIX relative paths ---

#: `(member as written, the name it is addressed by)`. A writer may spell
#: the same file several ways -- `tar -C dir .` and `shutil.make_archive`
#: prefix `./`, some writers emit `//` or `/./` -- and a `..` that stays
#: inside the archive is resolved like any URI reference.
_NORMALIZED_MEMBERS = [
    ("plain.txt", "plain.txt"),
    ("./dot-slash.txt", "dot-slash.txt"),
    ("dir//double.txt", "dir/double.txt"),
    ("dir/./interior.txt", "dir/interior.txt"),
    ("dir/sub/../up.txt", "dir/up.txt"),
    ("./a/./b//c.txt", "a/b/c.txt"),
]


@pytest.mark.parametrize("fmt", ["zip", "tar"])
def test_member_names_are_normalized_for_listing_and_lookup(tmp_path, fmt):
    """However the member was spelled, it lists and is read under its
    normalized name -- and the spelling as written still addresses it."""
    members = [(raw, raw.encode()) for raw, _ in _NORMALIZED_MEMBERS]
    write = _write_zip if fmt == "zip" else _write_tar
    archive = write(tmp_path / f"spellings.{fmt}", members)
    root = UriPath(_URI[fmt](archive))

    listed = sorted(p.path for p in root.rglob("*"))
    # The files under their normalized names, plus the directories those
    # imply. No "dir/sub": "dir/sub/../up.txt" resolves to "dir/up.txt".
    assert listed == [
        "a",
        "a/b",
        "a/b/c.txt",
        "dir",
        "dir/double.txt",
        "dir/interior.txt",
        "dir/up.txt",
        "dot-slash.txt",
        "plain.txt",
    ]
    for raw, normalized in _NORMALIZED_MEMBERS:
        assert (root / normalized).read_bytes() == raw.encode(), normalized
        assert (root / raw).read_bytes() == raw.encode(), raw


@pytest.mark.parametrize("fmt", ["zip", "tar"])
def test_every_listed_member_can_be_read(tmp_path, fmt):
    """Listing and lookup agree: a name a listing yields always resolves,
    and resolves to the member that name was normalized from. Zip used to
    hide `./x` from listings while still reading it by its raw name."""
    members = [(raw, raw.encode()) for raw, _ in _NORMALIZED_MEMBERS]
    members += [(name, b"evil") for name in _UNSAFE_MEMBER_NAMES]
    write = _write_zip if fmt == "zip" else _write_tar
    archive = write(tmp_path / f"mixed.{fmt}", members)
    root = UriPath(_URI[fmt](archive))

    expected = {normalized: raw for raw, normalized in _NORMALIZED_MEMBERS}
    read = {}
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        read[p.path] = p.read_bytes().decode()
    assert read == expected


#: Names with no normalized form inside the archive: they are neither listed
#: nor readable, whatever the spelling.
_UNSAFE_MEMBER_NAMES = [
    "../escape.txt",
    "pkg/../../escape.txt",
    "/abs.txt",
]

#: Legal POSIX filenames that a Windows *destination* would read as a drive.
#: They are real members: the rule against joining them onto a destination
#: belongs to that destination (checked per target in
#: `Path.copy(recursive=True)`, `PathSyncer` and `unpack_archive()`), not to
#: the archive, which has no platform of its own.
_DRIVE_SHAPED_MEMBER_NAMES = ["C:drive.txt", "pkg/D:evil.txt"]

#: The same point for `\`, but tar only: `zipfile` rewrites `\` to `/` when
#: it stores a name, so `back\slash.txt` comes back as `back/slash.txt` and
#: never reaches this library as a backslash name at all.
_BACKSLASH_MEMBER_NAMES = ["back\\slash.txt", "pkg/..\\..\\back.txt"]


@pytest.mark.parametrize("fmt", ["zip", "tar"])
@pytest.mark.parametrize("name", _DRIVE_SHAPED_MEMBER_NAMES)
def test_drive_shaped_member_names_are_listed_and_readable(tmp_path, fmt, name):
    """Dropping these lost real files: an archive written on POSIX may hold
    `C:drive.txt`, which escapes nothing there."""
    write = _write_zip if fmt == "zip" else _write_tar
    archive = write(tmp_path / f"shaped.{fmt}", [(name, b"real"), ("ok.txt", b"ok")])
    root = UriPath(_URI[fmt](archive))
    assert name in [p.path for p in root.rglob("*")]
    # A bare join would re-parse a leading "C:" as a URI scheme (the
    # documented round trip), so address it as the header prescribes.
    assert root.with_segments(name).read_bytes() == b"real"


@pytest.mark.parametrize("name", _BACKSLASH_MEMBER_NAMES)
def test_backslash_member_names_are_listed_and_readable_in_a_tar(tmp_path, name):
    """`\\` is an ordinary filename character on POSIX and tar keeps it."""
    archive = _write_tar(tmp_path / "shaped.tar", [(name, b"real"), ("ok.txt", b"ok")])
    root = UriPath(_URI["tar"](archive))
    assert name in [p.path for p in root.rglob("*")]
    assert root.with_segments(name).read_bytes() == b"real"


@pytest.mark.parametrize("fmt", ["zip", "tar"])
@pytest.mark.parametrize("name", _UNSAFE_MEMBER_NAMES)
def test_escaping_member_names_are_not_listed(tmp_path, fmt, name):
    write = _write_zip if fmt == "zip" else _write_tar
    archive = write(tmp_path / f"escape.{fmt}", [(name, b"evil"), ("ok.txt", b"ok")])
    root = UriPath(_URI[fmt](archive))
    assert [p.path for p in root.rglob("*")] == ["ok.txt"]


@pytest.mark.parametrize("fmt", ["zip", "tar"])
@pytest.mark.parametrize(
    "name",
    # "C:drive.txt" is left out: joining it re-parses "C:" as a scheme (the
    # documented URI round trip), so it never reaches the archive at all.
    [n for n in _UNSAFE_MEMBER_NAMES if n[1:2] != ":"],
)
def test_escaping_member_names_are_not_readable(tmp_path, fmt, name):
    write = _write_zip if fmt == "zip" else _write_tar
    archive = write(tmp_path / f"escape.{fmt}", [(name, b"evil"), ("ok.txt", b"ok")])
    root = UriPath(_URI[fmt](archive))
    with pytest.raises(FileNotFoundError):
        (root / name).read_bytes()


# --- memutils-archive-windows-drive-escape ---

_UNSAFE_UNPACK_NAMES = [
    "../x",
    "/abs",
    "C:x",
    "D:/x",
    "C:../C:../x",
    "\\\\server\\share\\x",
]


@pytest.mark.parametrize("fmt", ["zip", "tar"])
@pytest.mark.parametrize("name", _UNSAFE_UNPACK_NAMES)
def test_unpack_archive_never_writes_outside_dest(tmp_path, monkeypatch, fmt, name):
    members = [(name, b"payload"), ("ok.txt", b"ok")]
    write = _write_zip if fmt == "zip" else _write_tar
    archive = LocalPath(write(tmp_path / f"crafted.{fmt}", members))
    top = tmp_path / "a"
    dest = top / "b" / "dest"
    dest.parent.mkdir(parents=True)

    # Record every write target: a different-drive escape ('D:x') cannot be
    # observed on disk when the machine has no such drive.
    touched = []
    real_open, real_mkdir = LocalPath.open, LocalPath.mkdir

    def recording_open(self, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            touched.append(str(self))
        return real_open(self, mode, *args, **kwargs)

    def recording_mkdir(self, *args, **kwargs):
        touched.append(str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(LocalPath, "open", recording_open)
    monkeypatch.setattr(LocalPath, "mkdir", recording_mkdir)
    unpack_archive(archive, LocalPath(dest))
    monkeypatch.undo()

    assert (dest / "ok.txt").read_bytes() == b"ok"
    inside = str(dest)
    assert all(t == inside or t.startswith(inside + os.sep) for t in touched), touched
    assert [p for p in top.rglob("*") if p.is_file() and dest not in p.parents] == []
    assert [p for p in tmp_path.iterdir() if p.is_file()] == [archive]


# --- ftparchive-zip-append-not-crash-safe ---


def _stray_files(directory):
    return [p.name for p in directory.iterdir() if p.name.startswith(".pathlib_next")]


def test_zip_interrupted_append_leaves_the_archive_intact(zip_archive, monkeypatch):
    before = zip_archive.read_bytes()

    def dies_before_the_central_directory(self):
        # The window in which an in-place append had already overwritten
        # the old central directory and not yet written the new one.
        raise OSError("simulated crash")

    monkeypatch.setattr(
        zipfile.ZipFile, "_write_end_record", dies_before_the_central_directory
    )
    with pytest.raises(OSError, match="simulated crash"):
        (UriPath(_zip_uri(zip_archive)) / "new.txt").write_bytes(b"x" * 100_000)
    monkeypatch.undo()
    _release()
    assert zip_archive.read_bytes() == before
    with zipfile.ZipFile(zip_archive) as zf:
        assert zf.namelist() == ["docs/readme.txt", "top.txt"]
        assert zf.read("docs/readme.txt") == b"hello world"
    assert _stray_files(zip_archive.parent) == []


def test_zip_append_keeps_member_bytes_and_archive_mode(zip_archive):
    with zipfile.ZipFile(zip_archive) as zf:
        raw_before = {i.filename: (i.CRC, i.compress_size) for i in zf.infolist()}
    (UriPath(_zip_uri(zip_archive)) / "new.txt").write_text("new")
    with zipfile.ZipFile(zip_archive) as zf:
        raw_after = {i.filename: (i.CRC, i.compress_size) for i in zf.infolist()}
        assert zf.read("new.txt") == b"new"
    assert raw_after.pop("new.txt")
    assert raw_after == raw_before
    assert _stray_files(zip_archive.parent) == []


# --- ftparchive-zip-stale-shared-handle ---


def test_zip_write_after_an_external_change_keeps_that_change(zip_archive):
    root = UriPath(_zip_uri(zip_archive))
    assert (root / "top.txt").exists()  # opens and caches the shared handle
    with zipfile.ZipFile(zip_archive, "a") as zf:
        zf.writestr("external.txt", "from another tool")
    assert (root / "external.txt").read_text() == "from another tool"
    (root / "mine.txt").write_text("mine")
    with zipfile.ZipFile(zip_archive) as zf:
        assert sorted(zf.namelist()) == [
            "docs/readme.txt",
            "external.txt",
            "mine.txt",
            "top.txt",
        ]


@pytest.mark.skipif(os.name != "nt", reason="drive letters are Windows-only")
def test_zip_spellings_of_one_file_share_one_backend(zip_archive):
    uri = _zip_uri(zip_archive)
    drive = uri.index(":/", len("zip:file:")) - 1
    lower = uri[:drive] + uri[drive].swapcase() + uri[drive + 1 :]
    a, b = UriPath(uri), UriPath(lower)
    assert a.backend is b.backend
    (a / "from_a.txt").write_text("a")
    (b / "from_b.txt").write_text("b")
    assert {"from_a.txt", "from_b.txt"} <= set(_names(zip_archive))


def test_zip_read_does_not_keep_the_archive_open(zip_archive):
    member = UriPath(_zip_uri(zip_archive, "top.txt"))
    assert member.read_text() == "top level"
    assert [p.name for p in member.parent.iterdir()] == ["docs", "top.txt"]
    os.remove(zip_archive)  # WinError 32 while a handle is held
    assert not zip_archive.exists()
    del member
    _release()


# --- ftparchive-tar-live-stream-race ---


def test_tar_member_stream_survives_reads_of_other_members(tmp_path):
    import gzip

    path = tmp_path / "big.tar.gz"
    payloads = {f"m{i}.bin": os.urandom(64_000) for i in range(4)}
    with tarfile.open(path, "w:gz") as tf:
        for name, data in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    assert gzip.open(path).read(1)
    root = UriPath(_tar_uri(path))
    first = (root / "m0.bin").open("rb")
    head = first.read(1000)
    assert (root / "m3.bin").read_bytes() == payloads["m3.bin"]
    assert head + first.read() == payloads["m0.bin"]
    first.close()


def test_tar_concurrent_reads_return_each_members_bytes(tmp_path):
    import sys
    import threading

    path = tmp_path / "race.tar.gz"
    payloads = {f"m{i}.bin": os.urandom(50_000) for i in range(6)}
    with tarfile.open(path, "w:gz") as tf:
        for name, data in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    root = UriPath(_tar_uri(path))
    failures = []

    def reader(name):
        member = root / name
        for _ in range(5):
            try:
                with member.open("rb") as f:
                    data = b"".join(iter(lambda: f.read(512), b""))
                if data != payloads[name]:
                    failures.append((name, "wrong bytes"))
            except Exception as error:  # noqa: BLE001 -- reported below
                failures.append((name, repr(error)))

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=reader, args=(n,)) for n in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    assert failures == []
