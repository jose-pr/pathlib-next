"""`file:` paths: the ancestors of a Windows drive path end at its drive root,
and a Windows path string is read with Windows separators (on Windows only).

Everything that names a UNC share or a drive is a pure-path check: nothing
here connects to a host.
"""

import os

import pytest

from pathlib_next import LocalPath
from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.file import FileUri

WINDOWS = os.name == "nt"
BACKSLASH = chr(92)
windows_only = pytest.mark.skipif(
    not WINDOWS, reason="drive letters and backslash separators are Windows rules"
)
posix_only = pytest.mark.skipif(
    WINDOWS, reason="a backslash is a separator only on Windows"
)


def _parent_chain(path):
    chain = []
    while True:
        up = path.parent
        if up.as_uri() == path.as_uri():
            return chain
        chain.append(up)
        path = up


def _same_chain(path):
    assert [p.as_uri() for p in path.parents] == [
        p.as_uri() for p in _parent_chain(path)
    ]
    assert len(path.parents) == len(_parent_chain(path))
    assert [p.as_uri() for p in path.parents[::-1]] == [
        p.as_uri() for p in reversed(_parent_chain(path))
    ]


# --- parents follow parent ---------------------------------------------------

_SHAPES = {
    "posix": ("file:///usr/lib/python/site", True),
    "posix top level": ("file:///usr", True),
    "posix root": ("file:///", True),
    "unc share": ("file://server/share/a/b", True),
    "unc share root": ("file://server/share", True),
    "relative": ("file:a/b/c", True),
    "drive": ("file:///C:/Windows/System32/drivers", WINDOWS),
    "drive top level": ("file:///C:/Windows", WINDOWS),
    "drive root": ("file:///C:/", WINDOWS),
    "bare drive": ("file:///C:", WINDOWS),
    "drive under a host": ("file://localhost/C:/Windows/System32", WINDOWS),
}


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_parents_equal_the_repeated_parent(shape):
    uri, applies = _SHAPES[shape]
    if not applies:
        pytest.skip("a drive letter is an ordinary name off Windows")
    _same_chain(UriPath(uri))


def test_parents_of_a_posix_path_end_at_the_root():
    path = UriPath("file:///usr/lib/x")
    assert [p.as_uri() for p in path.parents] == [
        "file:/usr/lib",
        "file:/usr",
        "file:/",
    ]


@windows_only
def test_parents_of_a_drive_path_end_at_the_drive_root():
    path = UriPath(LocalPath("C:/Windows/System32/drivers"))
    parents = [p.as_uri() for p in path.parents]
    assert parents == ["file:/C:/Windows/System32", "file:/C:/Windows", "file:/C:/"]
    assert parents == [
        UriPath(p).as_uri() for p in LocalPath("C:/Windows/System32/drivers").parents
    ]
    assert [str(p.filepath) for p in path.parents] == [
        str(p) for p in LocalPath("C:/Windows/System32/drivers").parents
    ]
    assert path.parents[-1].is_absolute()
    assert path.parents[-2].parent == path.parents[-1]
    assert path.parents[-1].parent == path.parents[-1]


@windows_only
def test_no_ancestor_of_a_drive_path_is_relative_to_the_working_directory():
    path = UriPath("file:///C:/Windows/System32/drivers")
    for ancestor in path.parents:
        assert ancestor.is_absolute()
        assert str(ancestor.filepath).startswith("C:\\")
        probe = ancestor / "pyproject.toml"
        assert probe.is_absolute()
        assert probe.filepath.drive == "C:"


@windows_only
def test_parents_of_a_drive_path_under_a_host_end_at_the_drive_root():
    path = UriPath("file://localhost/C:/Windows/System32")
    assert [p.as_uri() for p in path.parents] == [
        "file://localhost/C:/Windows",
        "file://localhost/C:/",
    ]


@windows_only
def test_the_parent_of_a_bare_drive_is_the_drive():
    drive = UriPath("file:///C:")
    assert drive.parent == drive
    assert list(drive.parents) == []


# --- a Windows path string ----------------------------------------------------


@windows_only
def test_a_windows_path_string_joins_with_backslash_separators(tmp_path):
    base = FileUri(tmp_path.as_uri())
    joined = base / ("sub" + BACKSLASH + "x")
    assert joined.name == "x"
    assert joined.parent == base / "sub"
    assert joined == base / "sub" / "x"
    assert base.joinpath("a" + BACKSLASH + "b", "c").path == (base / "a/b/c").path
    assert (base / ("a" + BACKSLASH + ".." + BACKSLASH + "b")).path == (base / "b").path


@windows_only
def test_a_windows_absolute_path_string_restarts_the_join(tmp_path):
    base = FileUri(tmp_path.as_uri())
    drive_path = "C:" + BACKSLASH + "Temp" + BACKSLASH + "x"
    assert (base / drive_path).path == "C:/Temp/x"
    assert (base / drive_path) == UriPath(LocalPath("C:/Temp/x"))
    assert (base / (BACKSLASH + "abs")).path == "/abs"
    assert (base / ("D:" + BACKSLASH)).path == "D:/"


@windows_only
def test_str_of_a_local_path_is_a_destination_for_copy_move_and_rename(tmp_path):
    source = FileUri((tmp_path / "a.txt").as_uri())
    source.write_text("payload")

    copied = tmp_path / "copied.txt"
    source.copy(str(copied))
    assert copied.read_text() == "payload"

    moved = tmp_path / "moved.txt"
    source.move(str(moved))
    assert moved.read_text() == "payload"
    assert not (tmp_path / "a.txt").exists()

    renamed = tmp_path / "renamed.txt"
    FileUri(moved.as_uri()).rename(str(renamed))
    assert renamed.read_text() == "payload"
    assert not moved.exists()

    deep = tmp_path / "sub" / "deep.txt"
    deep.parent.mkdir()
    FileUri(renamed.as_uri()).rename(str(deep))
    assert deep.read_text() == "payload"


@posix_only
def test_a_backslash_is_an_ordinary_filename_character_off_windows(tmp_path):
    base = FileUri(tmp_path.as_uri())
    joined = base / ("a" + BACKSLASH + "b")
    assert joined.name == "a" + BACKSLASH + "b"
    assert joined.parent == base
    assert (base / (BACKSLASH + "abs")).parent == base

    source = FileUri((tmp_path / "src.txt").as_uri())
    source.write_text("payload")
    source.copy("we" + BACKSLASH + "ird.txt")
    assert (tmp_path / ("we" + BACKSLASH + "ird.txt")).read_text() == "payload"
    source.rename("an" + BACKSLASH + "other.txt")
    assert (tmp_path / ("an" + BACKSLASH + "other.txt")).read_text() == "payload"


def test_another_scheme_splits_a_key_on_slashes_only():
    base = UriPath("sftp://host/mnt/dir/")
    joined = base / ("a" + BACKSLASH + "b")
    assert joined.name == "a" + BACKSLASH + "b"
    assert joined.parent.path == "/mnt/dir"
    source = UriPath("sftp://host/mnt/dir/f.txt")
    for target in ("C:" + BACKSLASH + "Temp" + BACKSLASH + "x", "a" + BACKSLASH + "b"):
        assert source._coerce_target(target).parent.path == "/mnt/dir"
        assert source._rename_target(target).parent.path == "/mnt/dir"


# --- PathSyncer refuses one directory under two classes ---


def test_sync_overlap_sees_a_local_path_and_its_file_uri(tmp_path):
    from pathlib_next.utils.sync import PathSyncer, _paths_overlap

    tree = tmp_path / "tree"
    (tree / "inner").mkdir(parents=True)
    (tree / "keep.txt").write_text("keep")
    local = LocalPath(tree)
    as_uri = UriPath(tree.as_uri())
    assert isinstance(as_uri, FileUri)

    assert _paths_overlap(local, as_uri) and _paths_overlap(as_uri, local)
    assert _paths_overlap(local / "inner", as_uri)
    assert _paths_overlap(as_uri / "inner", local)
    assert not _paths_overlap(local / "inner", as_uri / "keep.txt")
    with pytest.raises(ValueError):
        PathSyncer(lambda entry: entry.stat.st_size, remove_missing=True).sync(
            local, as_uri
        )
    assert (tree / "keep.txt").read_text() == "keep"
