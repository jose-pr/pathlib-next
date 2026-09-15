"""Regression lock-in for currently-correct behavior (safety net).

These are the snippets from README.md's Quick start and examples/example.py
that don't touch the network, plus the no-extras contract: `pip install
pathlib-next` with no extras must import, and local paths, `MemPath` and the
`uripath` CLI must work. This module therefore imports nothing optional at
module level; URI tests skip when the `uri` extra is absent (the No-Extras CI
job runs this file in exactly that environment).
"""

import subprocess
import sys
import textwrap

import pytest

import pathlib_next
from pathlib_next import Path, glob
from pathlib_next.mempath import MemPath


def _uri():
    """The `pathlib_next.uri` module, or skip when the `uri` extra is absent."""
    pytest.importorskip("uritools")
    pytest.importorskip("netimps")
    import pathlib_next.uri

    return pathlib_next.uri


_BLOCK_URI_EXTRA = (
    'import sys\nsys.modules["uritools"] = sys.modules["netimps"] = None\n'
)


def _run_python(code: str, *, block_uri_extra: bool = False) -> str:
    """Run `code` in a fresh interpreter (clean `sys.modules`), return stdout.
    `block_uri_extra` hides `uritools`/`netimps` first, as a no-extras
    install would."""
    code = textwrap.dedent(code)
    if block_uri_extra:
        code = _BLOCK_URI_EXTRA + code
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_import_bare():
    import pathlib_next  # noqa: F401


def test_import_without_uri_extra_keeps_core_api():
    out = _run_python(
        """
        import pathlib_next
        from pathlib_next import LocalPath, Path, glob, sync
        from pathlib_next.mempath import MemPath

        assert not hasattr(pathlib_next, "UriPath")
        path = MemPath("a") / "b.txt"
        path.parent.mkdir(parents=True)
        path.write_text("x")
        print(path.read_text(), isinstance(Path("."), LocalPath))
        """,
        block_uri_extra=True,
    )
    assert out.split() == ["x", "True"]


def test_uripath_cli_reads_local_file_without_uri_extra(tmp_path):
    source = tmp_path / "input.txt"
    source.write_bytes(b"local bytes")
    out = _run_python(
        f"""
        import io
        from pathlib_next.tools import uripath

        stdout, stderr = io.BytesIO(), io.StringIO()
        assert uripath.main(["read", {str(source)!r}], stdout=stdout) == 0
        assert stdout.getvalue() == b"local bytes"
        code = uripath.main(["read", "http://example.com/x"], stderr=stderr)
        print(code)
        print(stderr.getvalue())
        """,
        block_uri_extra=True,
    )
    code, message = out.split("\n", 1)
    assert code == "1"
    assert "ImportError" in message
    assert "pathlib-next[uri]" in message


def test_import_does_not_load_netimps_or_unrelated_backends():
    _uri()
    out = _run_python("""
        import sys
        import pathlib_next
        from pathlib_next.uri import UriPath

        print(sorted(m for m in ("netimps", "requests", "botocore") if m in sys.modules))
        UriPath("file:/x")
        UriPath("data:,x")
        print(sorted(m for m in ("requests", "botocore") if m in sys.modules))
        """)
    assert out.split("\n")[:2] == ["[]", "[]"]


def test_import_optional_submodules_guarded():
    # Every scheme with an optional dependency is resolved lazily by
    # uri/schemes/__init__.py; importing the package must not require
    # requests/paramiko to be installed.
    _uri()
    import pathlib_next.uri.schemes  # noqa: F401


def test_readme_local_path():
    local_path = Path("./my_folder")
    assert isinstance(local_path, pathlib_next.LocalPath)


def test_readme_http_path_construct_only():
    uri = _uri()
    pytest.importorskip("requests")
    http_path = uri.UriPath("http://example.com/data.txt")
    assert http_path.source.scheme == "http"


def test_example_uri_child_join():
    uri = _uri()
    rootless = uri.Uri("sftp://root@sftpexample")
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
    uri = _uri()
    query = uri.Query({"test": "://$#!1", "test2&": [1, 2]})
    assert str(query) == "test=://$%23!1&test2%26=1&test2%26=2"
    q2 = uri.Query(str(query)).to_dict()
    assert q2 == {"test": ["://$#!1"], "test2&": ["1", "2"]}
    assert list(query) == [("test", "://$#!1"), ("test2&", "1"), ("test2&", "2")]


def test_example_source():
    uri = _uri()
    src = uri.Source(scheme="scheme", userinfo="user", host="123.com", port=0)
    assert {**src} == {
        "scheme": "scheme",
        "userinfo": "user",
        "host": "123.com",
        "port": 0,
    }
    assert [*src] == ["scheme", "user", "123.com", 0]


def test_example_uripath_norm():
    UriPath = _uri().UriPath
    dest = UriPath("file:./_ssh")
    with_dots = UriPath("a/b/c/d/../../test/.")
    assert with_dots.normalized_path == "a/b/test"

    source_host = UriPath("file://test.com/path1/path2/path3/path4")
    rel_to = source_host.relative_to("/path1/path2")
    assert rel_to.as_posix() == "path3/path4"
    dest = UriPath(dest)
    assert str(UriPath("file:") / "test") == "file:test"
    assert str(UriPath()) == ""
    assert dest.as_uri() == "file:_ssh"

    test1 = dest / "test" / "test2/"
    assert str(test1) == "file:_ssh/test/test2/"


def test_example_uripath_sftp_join():
    UriPath = _uri().UriPath
    pytest.importorskip("paramiko")
    sftp_root = UriPath("sftp://root@sftpexample/")
    assert sftp_root.as_posix() == "root@sftpexample:/"
    authkeys = sftp_root / "root/.ssh/authorized_keys"
    assert authkeys.as_posix() == "root@sftpexample:/root/.ssh/authorized_keys"


def test_example_glob_local(tmp_path):
    UriPath = _uri().UriPath
    (tmp_path / "a.py").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("")
    glob_test = UriPath(f"file:{tmp_path.as_posix()}/**/*.py")
    found = list(glob.glob(glob_test, recursive=True))
    assert len(found) == 2


def test_optional_schemes_presence_or_absence():
    _uri()
    from pathlib_next.uri import schemes

    # Check http
    try:
        import requests  # noqa: F401

        assert hasattr(schemes, "HttpPath")
    except ImportError:
        assert not hasattr(schemes, "HttpPath")

    # Check sftp: as of 0.8.2 importing SftpPath no longer requires an SSH
    # backend (paramiko/asyncssh) -- those are resolved lazily at USE time. The
    # scheme is a UriPath, so its import gate is the `uri` extra, which
    # `_uri()` above already required. Using it without any SSH backend
    # installed is what raises (covered by the backend-selection tests), not
    # importing it.
    assert hasattr(schemes, "SftpPath")

    # Check s3 -- S3Path only needs botocore at import time (boto3 itself is
    # a lazy import inside S3Backend.client()), so that's what gates it.
    try:
        import botocore  # noqa: F401

        assert hasattr(schemes, "S3Path")
    except ImportError:
        assert not hasattr(schemes, "S3Path")

    # Check webdav
    try:
        import requests  # noqa: F401

        assert hasattr(schemes, "DavPath")
    except ImportError:
        assert not hasattr(schemes, "DavPath")

    # Unknown names still raise AttributeError, and submodules still import.
    assert not hasattr(schemes, "NoSuchPath")
    from pathlib_next.uri.schemes import file as file_module

    assert schemes.FileUri is file_module.FileUri
