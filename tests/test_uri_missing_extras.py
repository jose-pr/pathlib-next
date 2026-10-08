"""A scheme class constructs, joins and compares without its client library,
and the first operation that needs the library raises an ImportError that
names the extra to install. Each case runs in a fresh interpreter that blocks
the import (`sys.modules[name] = None`), so nothing is uninstalled and no
socket is opened: the child raises on any connect or lookup."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("uritools")
pytest.importorskip("netimps")

_PRELUDE = """
import socket, sys

def _refuse(*args, **kwargs):
    raise RuntimeError("network used")

socket.socket.connect = _refuse
socket.getaddrinfo = _refuse
for name in {blocked!r}:
    sys.modules[name] = None
"""


_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def _run(code: str, blocked=()) -> subprocess.CompletedProcess:
    script = _PRELUDE.format(blocked=tuple(blocked)) + textwrap.dedent(code)
    env = dict(os.environ)
    # The checkout under test, not whichever copy is installed.
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(_SRC), env.get("PYTHONPATH")])
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


#: scheme URL, modules to hide, the extra its message must name.
_CASES = [
    ("http://h/x", ["requests"], "http"),
    ("https://h/x", ["requests"], "http"),
    ("dav://h/x", ["requests"], "http"),
    ("davs://h/x", ["requests"], "http"),
    ("github://github.com/o/r/dir/f.txt", ["requests"], "http"),
    ("gitlab://gitlab.com/o/r/dir/f.txt", ["requests"], "http"),
    ("git://github.com/o/r/dir/f.txt", ["requests"], "http"),
    ("git+gitlab://gitlab.com/o/r/dir/f.txt", ["requests"], "http"),
    ("s3://b/k", ["botocore", "boto3"], "s3"),
    ("gs://b/k", ["google", "google.cloud", "google.cloud.storage"], "gs"),
    (
        "az://acct/c/k",
        ["azure", "azure.storage", "azure.storage.blob", "azure.identity"],
        "az",
    ),
]


@pytest.mark.parametrize("url, blocked, extra", _CASES)
def test_a_path_without_its_client_library_does_pure_path_work(url, blocked, extra):
    result = _run(
        f"""
        from pathlib_next.uri import UriPath
        path = UriPath({url!r})
        child = path / "y"
        assert child.name == "y" and child.parent == path
        assert child.as_uri().startswith({url.split("://")[0] + "://"!r})
        assert path == UriPath({url!r}) and hash(path) == hash(UriPath({url!r}))
        assert path.relative_to(path).as_posix() in ("", ".")
        assert (path.with_name("z") if path.name else path).as_uri()
        print(type(path).__name__)
        """,
        blocked,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


@pytest.mark.parametrize("url, blocked, extra", _CASES)
def test_the_first_operation_that_needs_the_library_names_the_extra(
    url, blocked, extra
):
    result = _run(
        f"""
        from pathlib_next.uri import UriPath
        path = UriPath({url!r})
        for operation in ("exists", "stat", "read_bytes", "iterdir"):
            try:
                outcome = getattr(path, operation)()
                if operation == "iterdir":
                    list(outcome)
            except ImportError as error:
                message = str(error)
                assert 'pathlib-next[{extra}]' in message, message
                assert "{extra}" in message
                print(operation, "ImportError")
            else:
                raise SystemExit(operation + " did not raise ImportError")
        """,
        blocked,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "exists",
        "ImportError",
        "stat",
        "ImportError",
        "read_bytes",
        "ImportError",
        "iterdir",
        "ImportError",
    ]


def test_the_error_names_the_missing_package():
    result = _run(
        """
        from pathlib_next.uri import UriPath
        try:
            UriPath("http://h/x").stat()
        except ImportError as error:
            print(error.name)
            print(error.__cause__ is not None)
        """,
        ["requests"],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["requests", "True"]


def test_schemes_that_need_no_extra_work_with_every_client_blocked():
    result = _run(
        """
        import pathlib_next.uri.schemes as schemes
        from pathlib_next.uri import UriPath

        for name in ("HttpPath", "DavPath", "S3Path", "GsPath", "AzPath",
                     "GitHubPath", "GitLabPath", "GitPath", "FtpPath", "SftpPath",
                     "ZipUri", "TarUri", "FileUri", "DataUri"):
            assert hasattr(schemes, name), name
        assert UriPath("data:,abc").read_bytes() == b"abc"
        print("ok")
        """,
        [
            "requests",
            "urllib3",
            "botocore",
            "boto3",
            "google.cloud.storage",
            "azure.storage.blob",
            "paramiko",
            "asyncssh",
        ],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_sftp_without_either_library_names_both_extras():
    result = _run(
        """
        from pathlib_next.uri import UriPath
        path = UriPath("sftp://h/x")
        assert (path / "y").as_uri() == "sftp://h/x/y"
        try:
            path.exists()
        except ImportError as error:
            print(error)
        """,
        ["paramiko", "asyncssh"],
    )
    assert result.returncode == 0, result.stderr
    assert "'sftp-async'" in result.stdout and "'sftp'" in result.stdout


# --- the uri extra itself ---------------------------------------------------


def test_without_the_uri_extra_the_root_says_which_extra_provides_uripath():
    result = _run(
        """
        import pathlib_next
        assert not hasattr(pathlib_next, "UriPath")
        assert not hasattr(pathlib_next, "Uri")
        try:
            pathlib_next.UriPath
        except AttributeError as error:
            print(error)
        try:
            import pathlib_next.uri
        except ImportError as error:
            print(type(error).__name__, error)
        try:
            from pathlib_next import UriPath
        except ImportError as error:
            print("from-import", type(error).__name__)
        assert "UriPath" not in pathlib_next.__all__
        """,
        ["uritools", "netimps"],
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert 'pip install "pathlib-next[uri]"' in lines[0]
    assert "UriPath" in lines[0]
    assert lines[1].startswith("ModuleNotFoundError")
    assert 'pip install "pathlib-next[uri]"' in lines[1]
    assert lines[2] == "from-import ImportError"


def test_a_missing_attribute_of_the_root_is_still_an_ordinary_attribute_error():
    result = _run(
        """
        import pathlib_next
        try:
            pathlib_next.NoSuchName
        except AttributeError as error:
            print(error)
        """,
        ["uritools", "netimps"],
    )
    assert result.returncode == 0, result.stderr
    assert (
        result.stdout.strip() == "module 'pathlib_next' has no attribute 'NoSuchName'"
    )


def test_with_the_uri_extra_the_root_exports_are_ordinary_attributes():
    import pathlib_next

    assert pathlib_next.UriPath.__module__ == "pathlib_next.uri"
    assert "UriPath" in pathlib_next.__all__
    with pytest.raises(AttributeError, match="no attribute 'NoSuchName'"):
        pathlib_next.NoSuchName


def test_is_local_without_netimps_names_the_extra():
    result = _run(
        """
        from pathlib_next.uri import UriPath
        try:
            UriPath("sftp://some-host/x").is_local()
        except ImportError as error:
            print(error.name, 'pathlib-next[uri]' in str(error))
        """,
        ["netimps"],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["netimps", "True"]
