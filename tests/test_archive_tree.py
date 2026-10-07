"""Archive directories and member identity: a directory that exists only
through its members, one name with several spellings, a member under a file,
and the archive root. Each test reads the archive back with `zipfile`."""

import errno
import io
import tarfile
import warnings
import zipfile

import pytest

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes.archive.zip import _ZipBackend


def _zip_uri(path, inner=""):
    return f"zip:{path.as_uri()}!/{inner}"


def _write_zip(path, members):
    """`members`: (name, data) pairs; the names reach the archive raw."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate-name warnings
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in members:
                info = zipfile.ZipInfo("placeholder", date_time=(2001, 2, 3, 4, 5, 6))
                info.filename = name
                zf.writestr(info, data)
    return path


def _names(path):
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


@pytest.fixture
def rewrites(monkeypatch):
    """The number of times the archive was replaced on disk."""
    calls = []
    original = _ZipBackend._replace_outer

    def counted(self, fill):
        calls.append(1)
        return original(self, fill)

    monkeypatch.setattr(_ZipBackend, "_replace_outer", counted)
    return calls


# --- removing a tree --------------------------------------------------------


def test_rm_recursive_removes_an_implicit_directory_in_one_rewrite(tmp_path, rewrites):
    archive = _write_zip(
        tmp_path / "a.zip",
        [("imp/sub/a.txt", b"a"), ("imp/b.txt", b"b"), ("keep.txt", b"k")],
    )
    root = UriPath(_zip_uri(archive))
    (root / "imp").rm(recursive=True)
    assert _names(archive) == ["keep.txt"]
    assert not (root / "imp").exists()
    assert len(rewrites) == 1


def test_rm_recursive_removes_an_explicit_directory_with_its_marker(tmp_path, rewrites):
    archive = _write_zip(
        tmp_path / "a.zip",
        [
            ("d/", b""),
            ("d/x.txt", b"x"),
            ("d/e/", b""),
            ("d/e/y.txt", b"y"),
            ("k", b""),
        ],
    )
    root = UriPath(_zip_uri(archive))
    (root / "d").rm(recursive=True)
    assert _names(archive) == ["k"]
    assert len(rewrites) == 1


def test_rm_recursive_of_the_archive_root_empties_the_archive(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("imp/a.txt", b"a"), ("top.txt", b"t")])
    root = UriPath(_zip_uri(archive))
    root.rm(recursive=True)
    assert _names(archive) == []
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_rm_recursive_keeps_the_parent_of_what_it_removed(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("a/b/c.txt", b"c")])
    root = UriPath(_zip_uri(archive))
    (root / "a" / "b").rm(recursive=True)
    assert _names(archive) == ["a/"]
    assert (root / "a").is_dir()
    assert list((root / "a").iterdir()) == []


def test_rm_recursive_refuses_a_member_under_a_file_and_removes_nothing(tmp_path):
    archive = _write_zip(
        tmp_path / "a.zip", [("d/y", b"file"), ("d/y/z", b"under"), ("d/w", b"w")]
    )
    root = UriPath(_zip_uri(archive))
    with pytest.raises(OSError) as raised:
        (root / "d").rm(recursive=True)
    assert raised.value.errno == errno.ENOTEMPTY
    assert _names(archive) == ["d/y", "d/y/z", "d/w"]


def test_rm_recursive_reports_through_ignore_error(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("d/y", b"file"), ("d/y/z", b"under")])
    root = UriPath(_zip_uri(archive))
    seen = []
    (root / "d").rm(
        recursive=True, ignore_error=lambda error, path: seen.append(path) or True
    )
    assert seen == [root / "d"]
    assert _names(archive) == ["d/y", "d/y/z"]


def test_rm_recursive_of_a_missing_member_follows_missing_ok(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("k", b"")])
    root = UriPath(_zip_uri(archive))
    (root / "nope").rm(recursive=True, missing_ok=True)
    with pytest.raises(FileNotFoundError):
        (root / "nope").rm(recursive=True)
    assert _names(archive) == ["k"]


# --- removing a file never removes its parent -------------------------------


def test_unlink_of_the_last_file_keeps_an_implicit_parent(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("imp/a.txt", b"a"), ("k", b"")])
    root = UriPath(_zip_uri(archive))
    (root / "imp" / "a.txt").unlink()
    assert _names(archive) == ["k", "imp/"]
    assert (root / "imp").is_dir()
    (root / "imp" / "new.txt").write_text("again")
    assert (root / "imp" / "new.txt").read_text() == "again"


def test_unlink_with_siblings_left_adds_no_marker(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("imp/a.txt", b"a"), ("imp/b.txt", b"b")])
    (UriPath(_zip_uri(archive)) / "imp" / "a.txt").unlink()
    assert _names(archive) == ["imp/b.txt"]


def test_rmdir_of_a_directory_emptied_by_unlink_succeeds(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("a/b/c.txt", b"c")])
    root = UriPath(_zip_uri(archive))
    (root / "a" / "b" / "c.txt").unlink()
    (root / "a" / "b").rmdir()
    assert _names(archive) == ["a/"]
    (root / "a").rmdir()
    assert _names(archive) == []


def test_rmdir_of_the_root_needs_an_empty_archive(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("k", b"")])
    root = UriPath(_zip_uri(archive))
    with pytest.raises(OSError) as raised:
        root.rmdir()
    assert raised.value.errno == errno.ENOTEMPTY
    (root / "k").unlink()
    root.rmdir()
    assert root.is_dir()
    assert _names(archive) == []


def test_rename_out_of_a_directory_keeps_the_directory(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("imp/a.txt", b"a")])
    root = UriPath(_zip_uri(archive))
    (root / "imp" / "a.txt").rename(root / "out.txt")
    assert sorted(_names(archive)) == ["imp/", "out.txt"]
    assert (root / "imp").is_dir()


def test_rename_inside_a_directory_adds_no_marker(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("imp/a.txt", b"a")])
    (UriPath(_zip_uri(archive)) / "imp" / "a.txt").rename("b.txt")
    assert _names(archive) == ["imp/b.txt"]


# --- rename checks its destination ------------------------------------------


def test_rename_below_a_file_is_refused(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("a.txt", b"A"), ("b.txt", b"B")])
    root = UriPath(_zip_uri(archive))
    with pytest.raises(NotADirectoryError):
        (root / "a.txt").rename("b.txt/inside.txt")
    assert _names(archive) == ["a.txt", "b.txt"]


def test_rename_below_a_missing_directory_is_refused(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("a.txt", b"A")])
    root = UriPath(_zip_uri(archive))
    with pytest.raises(FileNotFoundError):
        (root / "a.txt").rename("nodir/x.txt")
    assert _names(archive) == ["a.txt"]


def test_rename_of_a_directory_into_itself_is_refused(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("d/x", b"x")])
    root = UriPath(_zip_uri(archive))
    with pytest.raises(OSError) as raised:
        (root / "d").rename("d/sub")
    assert raised.value.errno == errno.EINVAL
    assert _names(archive) == ["d/x"]


def test_rename_of_a_missing_member_is_not_found(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("k", b"")])
    with pytest.raises(FileNotFoundError):
        (UriPath(_zip_uri(archive)) / "nope").rename("other")
    assert _names(archive) == ["k"]


# --- one name, several spellings --------------------------------------------


def test_unlink_removes_every_spelling_of_the_name(tmp_path):
    archive = _write_zip(
        tmp_path / "a.zip", [("norm", b"norm-1"), ("./norm", b"norm-2")]
    )
    root = UriPath(_zip_uri(archive))
    (root / "norm").unlink()
    assert _names(archive) == []
    assert not (root / "norm").exists()
    assert list(root.iterdir()) == []


def test_rename_moves_every_spelling_and_the_winning_content(tmp_path):
    archive = _write_zip(
        tmp_path / "a.zip", [("norm", b"norm-1"), ("./norm", b"norm-2")]
    )
    root = UriPath(_zip_uri(archive))
    (root / "norm").rename("n2")
    assert _names(archive) == ["n2"]
    assert not (root / "norm").exists()
    assert (root / "n2").read_bytes() == b"norm-2"


def test_rename_over_a_name_with_two_spellings_replaces_them_all(tmp_path):
    archive = _write_zip(
        tmp_path / "a.zip", [("src", b"SRC"), ("dst", b"old-1"), ("./dst", b"old-2")]
    )
    root = UriPath(_zip_uri(archive))
    (root / "src").rename("dst")
    assert _names(archive) == ["dst"]
    assert (root / "dst").read_bytes() == b"SRC"


def test_rmdir_removes_every_spelling_of_a_directory_marker(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("d/", b""), ("./d/", b"")])
    root = UriPath(_zip_uri(archive))
    (root / "d").rmdir()
    assert _names(archive) == []
    assert not (root / "d").exists()


def test_rm_recursive_removes_every_spelling_under_a_directory(tmp_path):
    archive = _write_zip(
        tmp_path / "a.zip",
        [("d/x", b"1"), ("./d/x", b"2"), ("./d/", b""), ("d/y", b"y"), ("k", b"")],
    )
    root = UriPath(_zip_uri(archive))
    (root / "d").rm(recursive=True)
    assert _names(archive) == ["k"]


def test_rename_of_a_directory_with_two_spellings_moves_all(tmp_path):
    archive = _write_zip(
        tmp_path / "a.zip", [("d/x", b"1"), ("./d/x", b"2"), ("d/y", b"y")]
    )
    root = UriPath(_zip_uri(archive))
    (root / "d").rename("e")
    assert _names(archive) == ["e/x", "e/y"]
    assert not (root / "d").exists()
    assert (root / "e" / "x").read_bytes() == b"2"


# --- a member under a file does not exist ------------------------------------


def test_member_under_a_file_is_absent_for_every_lookup(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("y", b"file"), ("y/z", b"under")])
    root = UriPath(_zip_uri(archive))
    below = root / "y" / "z"
    assert (root / "y").is_file()
    assert not below.exists()
    assert not below.is_file()
    assert not below.parent.is_dir()
    with pytest.raises(FileNotFoundError):
        below.stat()
    with pytest.raises(FileNotFoundError):
        below.read_bytes()
    with pytest.raises(NotADirectoryError):
        list((root / "y").iterdir())
    assert [p.path for p in root.glob("**/*")] == ["y"]


def test_member_under_a_file_cannot_be_removed_or_renamed(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("y", b"file"), ("y/z", b"under")])
    root = UriPath(_zip_uri(archive))
    below = root / "y" / "z"
    with pytest.raises(FileNotFoundError):
        below.unlink()
    with pytest.raises(FileNotFoundError):
        below.rename("w")
    assert _names(archive) == ["y", "y/z"]


def test_writing_below_a_file_is_refused(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("y", b"file")])
    root = UriPath(_zip_uri(archive))
    with pytest.raises(NotADirectoryError):
        (root / "y" / "z").write_text("x")
    with pytest.raises(NotADirectoryError):
        (root / "y" / "z").mkdir()
    assert _names(archive) == ["y"]


def test_tar_member_under_a_file_is_absent_too(tmp_path):
    archive = tmp_path / "a.tar"
    with tarfile.open(archive, "w") as tf:
        for name, data in [("y", b"file"), ("y/z", b"under"), ("d/x", b"x")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    root = UriPath(f"tar:{archive.as_uri()}!/")
    assert not (root / "y" / "z").exists()
    with pytest.raises(FileNotFoundError):
        (root / "y" / "z").read_bytes()
    assert sorted(p.path for p in root.glob("**/*")) == ["d", "d/x", "y"]


# --- the archive root is the archive ----------------------------------------


def test_root_of_a_missing_archive_does_not_exist(tmp_path):
    root = UriPath(_zip_uri(tmp_path / "missing.zip"))
    assert not root.exists()
    assert not root.is_dir()
    with pytest.raises(FileNotFoundError):
        root.stat()
    with pytest.raises(FileNotFoundError):
        list(root.iterdir())


def test_root_of_a_file_that_is_not_an_archive_raises_as_a_listing_does(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"not a zip file")
    root = UriPath(_zip_uri(notes))
    with pytest.raises(zipfile.BadZipFile):
        root.stat()
    with pytest.raises(zipfile.BadZipFile):
        root.exists()
    with pytest.raises(zipfile.BadZipFile):
        list(root.iterdir())
    assert notes.read_bytes() == b"not a zip file"


def test_root_of_a_tar_that_is_not_an_archive_raises_as_a_listing_does(tmp_path):
    notes = tmp_path / "notes.tar"
    notes.write_bytes(b"not a tar file")
    root = UriPath(f"tar:{notes.as_uri()}!/")
    with pytest.raises(tarfile.ReadError):
        root.stat()
    with pytest.raises(tarfile.ReadError):
        list(root.iterdir())


def test_root_of_a_missing_archive_cannot_be_made_a_directory(tmp_path):
    root = UriPath(_zip_uri(tmp_path / "missing.zip"))
    with pytest.raises(FileNotFoundError):
        root.mkdir()
    assert not (tmp_path / "missing.zip").exists()


def test_root_of_an_existing_archive_exists_already(tmp_path):
    archive = _write_zip(tmp_path / "a.zip", [("k", b"")])
    with pytest.raises(FileExistsError):
        UriPath(_zip_uri(archive)).mkdir()
    UriPath(_zip_uri(archive)).mkdir(exist_ok=True)


def test_root_of_an_archive_the_server_does_not_have_does_not_exist(http_status_server):
    root = UriPath(f"zip:{http_status_server}/404!/")
    assert not root.exists()
    with pytest.raises(FileNotFoundError):
        root.stat()
