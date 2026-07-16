"""Regression lock-in for currently-correct behavior (safety net).

These are the snippets from README.md's Quick start and examples/example.py
that don't touch the network. On Python 3.9 this file is expected to fail
until the Python 3.9 compatibility work lands.
"""
import pytest

import pathlib_next
from pathlib_next import Path, glob
from pathlib_next.mempath import MemPath
from pathlib_next.uri import Query, Source, Uri, UriPath


def test_import_bare():
    import pathlib_next  # noqa: F401


def test_import_optional_submodules_guarded():
    # http/sftp schemes are guarded by try/except ImportError in
    # uri/schemes/__init__.py; importing the package must not require
    # requests/paramiko to be installed.
    import pathlib_next.uri.schemes  # noqa: F401


def test_readme_local_path():
    local_path = Path("./my_folder")
    assert isinstance(local_path, pathlib_next.LocalPath)


def test_readme_http_path_construct_only():
    pytest.importorskip("requests")
    http_path = UriPath("http://example.com/data.txt")
    assert http_path.source.scheme == "http"


def test_example_uri_child_join():
    rootless = Uri("sftp://root@sftpexample")
    rootless.source
    authkeys = rootless / "root/.ssh/authorized_keys"
    keys = authkeys.as_uri()
    assert keys == "sftp://root@sftpexample/root/.ssh/authorized_keys"


def test_example_mempath_roundtrip(tmp_path):
    mempath = MemPath("test/test3") / "subpath"
    mempath.parent.mkdir(parents=True, exist_ok=True)
    mempath.write_text("test")
    check = mempath.read_text()
    assert check == "test"
    mempath.parent.rm(recursive=True)


def test_example_query():
    query = Query({"test": "://$#!1", "test2&": [1, 2]})
    q2 = Query(str(query)).to_dict()
    assert q2 is not None
    for name, value in query:
        pass


def test_example_source():
    src = Source(scheme="scheme", userinfo="user", host="123.com", port=0)
    test = {**src}
    test2 = [*src]
    assert test2


def test_example_uripath_norm():
    dest = UriPath("file:./_ssh")
    with_dots = UriPath("a/b/c/d/../../test/.")
    with_dots.normalized_path

    source_host = UriPath("file://test.com/path1/path2/path3/path4")
    rel_to = source_host.relative_to("/path1/path2")
    dest = UriPath(dest)
    test_ = UriPath("file:") / "test"
    empty = UriPath()
    uri = dest.as_uri()

    test1 = dest / "test" / "test2/"
    str(test1)


def test_example_uripath_sftp_join():
    pytest.importorskip("paramiko")
    sftp_root = UriPath("sftp://root@sftpexample/")
    sftp_root.as_posix()
    authkeys = sftp_root / "root/.ssh/authorized_keys"
    authkeys.as_posix()


def test_example_glob_local(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("")
    glob_test = UriPath(f"file:{tmp_path.as_posix()}/**/*.py")
    found = list(glob.glob(glob_test, recursive=True))
    assert len(found) == 2


def test_optional_schemes_presence_or_absence():
    from pathlib_next.uri import schemes
    # Check http
    try:
        import requests  # noqa: F401
        assert hasattr(schemes, "HttpPath")
    except ImportError:
        assert not hasattr(schemes, "HttpPath")

    # Check sftp
    try:
        import paramiko  # noqa: F401
        assert hasattr(schemes, "SftpPath")
    except ImportError:
        assert not hasattr(schemes, "SftpPath")

    # Check s3
    try:
        import boto3  # noqa: F401
        assert hasattr(schemes, "S3Path")
    except ImportError:
        assert not hasattr(schemes, "S3Path")

    # Check webdav
    try:
        import requests  # noqa: F401
        assert hasattr(schemes, "DavPath")
    except ImportError:
        assert not hasattr(schemes, "DavPath")


