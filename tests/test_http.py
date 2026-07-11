"""HttpPath against a real stdlib ThreadingHTTPServer (see conftest.py's
http_server fixture) serving fixture_tree's directory listing. Skipped
entirely if the http extra isn't installed.
"""
import pytest

pytest.importorskip("requests")
pytest.importorskip("bs4")
pytest.importorskip("htmllistparse")

from pathlib_next.uri import UriPath


def test_iterdir_lists_files_and_dirs(http_server):
    root = UriPath(f"{http_server}/")
    names = {p.name for p in root.iterdir()}
    # B-adjacent regression: subdirectory entries used to get name == ""
    # because directory-listing entries carry a trailing "/" that wasn't
    # stripped before deriving .name. iterdir() (unlike glob()) doesn't
    # filter hidden entries, matching pathlib.Path.iterdir().
    assert names == {"a.txt", "b.py", ".hidden.txt", "sub", "empty_dir"}


def test_iterdir_subdir_is_dir_not_file(http_server):
    root = UriPath(f"{http_server}/")
    sub = next(p for p in root.iterdir() if p.name == "sub")
    assert sub.is_dir()
    assert not sub.is_file()


def test_file_is_file_not_dir(http_server):
    root = UriPath(f"{http_server}/")
    f = next(p for p in root.iterdir() if p.name == "a.txt")
    assert f.is_file()
    assert not f.is_dir()


def test_read_text(http_server):
    p = UriPath(f"{http_server}/a.txt")
    assert p.read_text() == "a"


def test_stat_size(http_server):
    p = UriPath(f"{http_server}/a.txt")
    st = p.stat()
    assert st.st_size == 1  # fixture_tree's a.txt content is "a"


def test_stat_missing_raises_filenotfounderror(http_server):
    p = UriPath(f"{http_server}/does-not-exist.txt")
    with pytest.raises(FileNotFoundError):
        p.stat()


def test_exists_false_for_missing(http_server):
    p = UriPath(f"{http_server}/does-not-exist.txt")
    assert not p.exists()


def test_dir_without_trailing_slash_is_dir(http_server):
    # The server 301-redirects a bare "/sub" request to "/sub/" --
    # is_dir() must follow that and report True.
    p = UriPath(f"{http_server}/sub")
    assert p.is_dir()


def test_recursive_glob(http_server):
    root = UriPath(f"{http_server}/")
    names = {p.name for p in root.glob("**/*.py")}
    assert names == {"b.py", "c.py", "d.py"}
