import argparse
import errno
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pathlib_next import LocalPath
from pathlib_next.tools import uripath


def test_help_lists_commands(capsys):
    parser = uripath.build_parser()
    try:
        parser.parse_args(["--help"])
    except SystemExit as error:
        assert error.code == 0
    captured = capsys.readouterr()
    assert "read" in captured.out
    assert "sync" in captured.out


def test_read_local_file_to_stdout(tmp_path):
    path = tmp_path / "input.txt"
    path.write_bytes(b"hello")
    stdout = io.BytesIO()
    assert uripath.main(["read", str(path)], stdout=stdout) == 0
    assert stdout.getvalue() == b"hello"


def test_read_dash_copies_stdin_to_stdout():
    stdin = io.BytesIO(b"pipe")
    stdout = io.BytesIO()
    assert uripath.main(["read", "-"], stdin=stdin, stdout=stdout) == 0
    assert stdout.getvalue() == b"pipe"


def test_write_stdin_to_local_file(tmp_path):
    path = tmp_path / "output.txt"
    assert uripath.main(["write", str(path)], stdin=io.BytesIO(b"data")) == 0
    assert path.read_bytes() == b"data"


def test_write_argument_to_local_file(tmp_path):
    path = tmp_path / "output.txt"
    assert uripath.main(["write", str(path), "text"]) == 0
    assert path.read_text(encoding="utf-8") == "text"


def test_cp_local_file_to_local_file(tmp_path):
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_bytes(b"copy")
    assert uripath.main(["cp", str(source), str(target)]) == 0
    assert target.read_bytes() == b"copy"


def test_cp_stdin_to_local_file(tmp_path):
    target = tmp_path / "target.txt"
    assert uripath.main(["cp", "-", str(target)], stdin=io.BytesIO(b"copy")) == 0
    assert target.read_bytes() == b"copy"


def test_cp_local_file_to_stdout(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"copy")
    stdout = io.BytesIO()
    assert uripath.main(["cp", str(source), "-"], stdout=stdout) == 0
    assert stdout.getvalue() == b"copy"


def test_rm_recursive(tmp_path):
    root = tmp_path / "root"
    (root / "child").mkdir(parents=True)
    (root / "child" / "file.txt").write_text("x", encoding="utf-8")
    assert uripath.main(["rm", "--recursive", str(root)]) == 0
    assert not root.exists()


def test_sync_remove_missing(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "keep.txt").write_text("keep", encoding="utf-8")
    (target / "extra.txt").write_text("extra", encoding="utf-8")
    assert uripath.main(["sync", "--remove-missing", str(source), str(target)]) == 0
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (target / "extra.txt").exists()


def test_error_returns_one_and_writes_stderr(tmp_path):
    stderr = io.StringIO()
    missing = tmp_path / "missing.txt"
    assert uripath.main(["read", str(missing)], stderr=stderr) == 1
    assert "FileNotFoundError" in stderr.getvalue()


# --- gittools-cli-sync-size-only ---


def _sync_trees(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "version.txt").write_text("0.9.3", encoding="utf-8")
    (target / "version.txt").write_text("0.9.2", encoding="utf-8")
    return source, target


def test_sync_copies_same_size_edit(tmp_path):
    source, target = _sync_trees(tmp_path)
    assert uripath.main(["sync", str(source), str(target)]) == 0
    assert (target / "version.txt").read_text(encoding="utf-8") == "0.9.3"


def test_sync_size_only_keeps_size_comparison(tmp_path):
    source, target = _sync_trees(tmp_path)
    assert uripath.main(["sync", "--size-only", str(source), str(target)]) == 0
    assert (target / "version.txt").read_text(encoding="utf-8") == "0.9.2"


# --- gittools-cli-dry-run-silent ---


def test_sync_dry_run_prints_planned_changes(tmp_path):
    source, target = _sync_trees(tmp_path)
    (source / "new.txt").write_text("new", encoding="utf-8")
    (target / "stale.txt").write_text("stale", encoding="utf-8")
    stdout = io.BytesIO()
    argv = ["sync", "--dry-run", "--remove-missing", str(source), str(target)]
    assert uripath.main(argv, stdout=stdout) == 0
    lines = stdout.getvalue().decode("utf-8").splitlines()
    assert f"would copy {source / 'new.txt'} -> {target / 'new.txt'}" in lines
    assert f"would copy {source / 'version.txt'} -> {target / 'version.txt'}" in lines
    assert any(line.startswith("would remove ") for line in lines)
    # Nothing changed.
    assert not (target / "new.txt").exists()
    assert (target / "stale.txt").exists()
    assert (target / "version.txt").read_text(encoding="utf-8") == "0.9.2"


def test_sync_quiet_by_default_and_verbose_on_request(tmp_path):
    source, target = _sync_trees(tmp_path)
    stdout = io.BytesIO()
    assert uripath.main(["sync", str(source), str(target)], stdout=stdout) == 0
    assert stdout.getvalue() == b""
    (source / "version.txt").write_text("1.0.0", encoding="utf-8")
    assert uripath.main(["sync", "-v", str(source), str(target)], stdout=stdout) == 0
    expected = f"copy {source / 'version.txt'} -> {target / 'version.txt'}"
    assert stdout.getvalue().decode("utf-8").splitlines() == [expected]


# --- gittools-cli-reads-whole-object-into-memory ---


class _RecordingWriter(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.sizes = []

    def write(self, data):
        self.sizes.append(len(data))
        return super().write(data)


class _RecordingReader(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.sizes = []

    def read(self, size=-1):
        self.sizes.append(size)
        return super().read(size)


_BIG = bytes(range(256)) * (3 * 1024 * 1024 // 256 + 1)


def test_read_streams_in_chunks(tmp_path):
    path = tmp_path / "big.bin"
    path.write_bytes(_BIG)
    stdout = _RecordingWriter()
    assert uripath.main(["read", str(path)], stdout=stdout) == 0
    assert stdout.getvalue() == _BIG
    assert len(stdout.sizes) > 1
    assert max(stdout.sizes) <= uripath._CHUNK_SIZE


def test_cp_dash_streams_in_chunks(tmp_path):
    target = tmp_path / "target.bin"
    stdin = _RecordingReader(_BIG)
    assert uripath.main(["cp", "-", str(target)], stdin=stdin) == 0
    assert target.read_bytes() == _BIG
    assert stdin.sizes and all(0 < size <= uripath._CHUNK_SIZE for size in stdin.sizes)

    stdout = _RecordingWriter()
    assert uripath.main(["cp", str(target), "-"], stdout=stdout) == 0
    assert stdout.getvalue() == _BIG
    assert max(stdout.sizes) <= uripath._CHUNK_SIZE

    stdin = _RecordingReader(b"piped")
    stdout = _RecordingWriter()
    assert uripath.main(["read", "-"], stdin=stdin, stdout=stdout) == 0
    assert stdout.getvalue() == b"piped"
    assert all(0 < size <= uripath._CHUNK_SIZE for size in stdin.sizes)


# --- gittools-cli-cp-stdin-clobbers --------------------------------------------


def test_cp_stdin_refuses_existing_target_without_overwrite(tmp_path):
    target = tmp_path / "important.bin"
    target.write_bytes(b"precious")
    stderr = io.StringIO()
    rc = uripath.main(
        ["cp", "-", str(target)], stdin=io.BytesIO(b"clobbered"), stderr=stderr
    )
    assert rc == 1
    assert "FileExistsError" in stderr.getvalue()
    assert target.read_bytes() == b"precious"


def test_cp_stdin_overwrites_with_overwrite(tmp_path):
    target = tmp_path / "important.bin"
    target.write_bytes(b"precious")
    rc = uripath.main(
        ["cp", "--overwrite", "-", str(target)], stdin=io.BytesIO(b"replaced")
    )
    assert rc == 0
    assert target.read_bytes() == b"replaced"


def test_cp_recursive_with_dash_is_rejected(tmp_path, capsys):
    target = tmp_path / "t.bin"
    rc = uripath.main(["cp", "-r", "-", str(target)], stdin=io.BytesIO(b"x"))
    assert rc == 2
    assert "--recursive" in capsys.readouterr().err
    assert not target.exists()


def test_write_still_overwrites_by_design(tmp_path):
    target = tmp_path / "w.txt"
    target.write_bytes(b"old")
    assert uripath.main(["write", str(target), "new"]) == 0
    assert target.read_bytes() == b"new"


# --- gittools-cli-colon-filenames-treated-as-uris ------------------------------


@pytest.mark.parametrize(
    "value",
    ["12:30.txt", "2024-01-01T10:00:00.log", "notes:draft", "C:/x", "c:x", "./a:b"],
)
def test_colon_names_that_are_not_uris_stay_local(value):
    from pathlib_next import LocalPath

    assert not uripath._looks_like_uri(value)
    assert isinstance(uripath._path(value), LocalPath)


@pytest.mark.parametrize(
    "value",
    [
        "s3://bucket/key",
        "zip:file:///a.zip!/x",
        "data:,abc",
        "file:relative",
        "unknown-scheme://host/x",
    ],
)
def test_uri_arguments_are_uris(value):
    assert uripath._looks_like_uri(value)


def test_write_to_colon_name_writes_a_local_file(tmp_path, monkeypatch):
    # "note:draft" is not a valid Windows file name; the relative POSIX
    # spelling is exercised through `_path()` above, this runs where legal.
    if sys.platform == "win32":
        pytest.skip("':' is not allowed in Windows file names")
    monkeypatch.chdir(tmp_path)
    assert uripath.main(["write", "12:30.txt", "hi"]) == 0
    assert (tmp_path / "12:30.txt").read_bytes() == b"hi"


# --- gittools-cli-broken-pipe-and-interrupt ------------------------------------


class _ClosedPipe(io.BytesIO):
    def write(self, data):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def test_read_into_a_closed_pipe_exits_quietly_with_sigpipe_status(tmp_path):
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * (3 * uripath._CHUNK_SIZE))
    stderr = io.StringIO()
    rc = uripath.main(["read", str(source)], stdout=_ClosedPipe(), stderr=stderr)
    assert rc == 141
    assert stderr.getvalue() == ""


def test_write_argument_to_closed_pipe_exits_quietly():
    stderr = io.StringIO()
    rc = uripath.main(["write", "-", "data"], stdout=_ClosedPipe(), stderr=stderr)
    assert rc == 141
    assert stderr.getvalue() == ""


def test_broken_pipe_from_a_source_is_still_reported(monkeypatch):
    # Only stdout going away is quiet: a connection that breaks mid-copy is
    # an error.
    def failing(args, *, stdin=None, stdout=None):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(uripath, "_cmd_read", failing)
    stderr = io.StringIO()
    rc = uripath.main(["read", "x"], stdout=io.BytesIO(), stderr=stderr)
    assert rc == 1
    assert "BrokenPipeError" in stderr.getvalue()


def test_keyboard_interrupt_exits_130_without_traceback(monkeypatch):
    def interrupted(args, *, stdin=None, stdout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(uripath, "_cmd_cp", interrupted)
    stderr = io.StringIO()
    assert uripath.main(["cp", "a", "b"], stderr=stderr) == 130
    assert stderr.getvalue() == ""


_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _child_env():
    """The environment of a child interpreter that imports this checkout."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    return env


def test_real_pipe_closed_by_reader_exits_quietly(tmp_path):
    source = tmp_path / "big.bin"
    source.write_bytes(b"y" * (8 * uripath._CHUNK_SIZE))
    proc = subprocess.Popen(
        [sys.executable, "-m", "pathlib_next.tools.uripath", "read", str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
    )
    assert proc.stdout.read(10) == b"y" * 10
    proc.stdout.close()
    err = proc.stderr.read()
    proc.stderr.close()
    assert proc.wait(timeout=60) == 141
    assert b"Traceback" not in err
    assert b"BrokenPipeError" not in err


# The child waits for one byte on stdin before it runs the command, so the
# parent has closed its end of stdout before the command writes anything.
_GATED = (
    "import sys\n"
    "sys.stdin.buffer.read(1)\n"
    "from pathlib_next.tools.uripath import main\n"
    "raise SystemExit(main(sys.argv[1:]))\n"
)


def _run_with_the_reader_gone(argv, stdin=b""):
    proc = subprocess.Popen(
        [sys.executable, "-c", _GATED, *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
    )
    proc.stdout.close()
    try:
        proc.stdin.write(b"g" + stdin)
        proc.stdin.close()
    except OSError:
        pass  # The child exited before it read everything.
    try:
        err = proc.stderr.read()
        return proc.wait(timeout=60), err
    finally:
        proc.stderr.close()
        if not proc.stdin.closed:
            proc.stdin.close()


@pytest.mark.parametrize("size", [1, 64 * 1024, 3 * 1024 * 1024])
def test_read_exits_141_when_stdout_closed_before_the_first_byte(size):
    status, err = _run_with_the_reader_gone(["read", "-"], stdin=b"z" * size)
    assert err == b""
    assert status == 141


def test_write_argument_exits_141_when_stdout_closed_before_the_first_byte():
    status, err = _run_with_the_reader_gone(["write", "-", "hello"])
    assert err == b""
    assert status == 141


def test_sync_dry_run_exits_141_when_stdout_closed_before_the_first_byte(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    status, err = _run_with_the_reader_gone(
        ["sync", "--dry-run", str(source), str(target)]
    )
    assert err == b""
    assert status == 141
    assert not (target / "a.txt").exists()


class _InvalidArgumentPipe(io.BytesIO):
    """The stdout of Windows after the reader closed: EINVAL, not EPIPE."""

    def write(self, data):
        raise OSError(errno.EINVAL, "Invalid argument")

    def flush(self):
        raise OSError(errno.EINVAL, "Invalid argument")


def test_einval_from_stdout_is_a_closed_pipe_on_windows(monkeypatch):
    monkeypatch.setattr(uripath, "_WINDOWS", True)
    stderr = io.StringIO()
    argv = ["write", "-", "data"]
    assert uripath.main(argv, stdout=_InvalidArgumentPipe(), stderr=stderr) == 141
    assert stderr.getvalue() == ""


def test_einval_from_stdout_is_an_error_elsewhere(monkeypatch):
    monkeypatch.setattr(uripath, "_WINDOWS", False)
    stderr = io.StringIO()
    argv = ["write", "-", "data"]
    assert uripath.main(argv, stdout=_InvalidArgumentPipe(), stderr=stderr) == 1
    assert stderr.getvalue().startswith("uripath: OSError:")


# --- the error line goes to a text or a binary stderr ---


def test_error_line_goes_to_a_binary_stderr(tmp_path):
    stderr = io.BytesIO()
    missing = tmp_path / "missing.txt"
    rc = uripath.main(["read", str(missing)], stdout=io.BytesIO(), stderr=stderr)
    assert rc == 1
    assert stderr.getvalue().startswith(b"uripath: FileNotFoundError: ")
    assert stderr.getvalue().endswith(b"\n")


# --- a wrong invocation is reported by the parser ---


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["nope"],
        ["read"],
        ["read", "a", "b"],
        ["write", "f", "x", "--encoding", "no-such-codec"],
        ["write", "f", "x", "--encoding", "hex"],
        ["cp", "-r", "-", "x"],
        ["cp", "-r", "x", "-"],
    ],
)
def test_wrong_invocation_returns_2_and_changes_nothing(
    argv, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    assert uripath.main(argv, stdin=io.BytesIO(b"q"), stdout=io.BytesIO()) == 2
    assert "usage: uripath" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_help_returns_zero_instead_of_exiting(capsys):
    assert uripath.main(["read", "--help"]) == 0
    assert "file or URI to read" in capsys.readouterr().out


def test_every_flag_and_positional_has_help_text():
    parser = uripath.build_parser()
    (subcommands,) = [
        action.choices
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert set(subcommands) == {"read", "write", "rm", "cp", "sync"}
    for name, subparser in subcommands.items():
        for action in subparser._actions:
            if not isinstance(action, argparse._HelpAction):
                assert action.help, f"{name}: {action.dest} has no help text"


# --- without the `uri` extra, only shipped schemes are URIs ---------------


@pytest.fixture
def without_uri_extra(monkeypatch):
    """The CLI as installed without the `uri` extra: no scheme class can be
    imported, so `_looks_like_uri()` falls back to the distribution's own
    entry points."""
    monkeypatch.setattr(uripath, "UriPath", None)
    monkeypatch.setattr(uripath, "_SHIPPED_SCHEMES", None)


@pytest.mark.parametrize("value", ["12:30.txt", "notes:draft", "2024-01-01T10:00.log"])
def test_colon_names_stay_local_without_the_uri_extra(value, without_uri_extra):
    """Regression: the no-extras CI job reported `notes:draft` as a URI
    needing an extra, because no scheme class could be loaded to say
    otherwise. Entry points answer that without importing anything."""
    assert not uripath._looks_like_uri(value)
    assert isinstance(uripath._path(value), LocalPath)


@pytest.mark.parametrize("value", ["s3:bucket/key", "data:,abc", "zip:x.zip!/a"])
def test_shipped_schemes_are_uris_without_the_uri_extra(value, without_uri_extra):
    assert uripath._looks_like_uri(value)
    with pytest.raises(ImportError, match="uri"):
        uripath._path(value)


def test_shipped_schemes_reads_this_distribution(without_uri_extra):
    schemes = uripath._shipped_schemes()
    assert {"file", "data", "zip", "s3", "sftp"} <= schemes
    assert "notes" not in schemes


def test_unknown_scheme_with_an_authority_is_still_a_uri(without_uri_extra):
    assert uripath._looks_like_uri("notascheme://host/x")


# --- a dry run fails where the real run fails ---


def test_sync_dry_run_fails_for_a_link_the_target_cannot_hold_like_the_real_run(
    tmp_path, monkeypatch
):
    from pathlib_next.mempath import MemPath

    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("aaa", encoding="utf-8")
    try:
        (source / "lnk").symlink_to("a.txt")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")
    target = MemPath("/target")
    target.mkdir()
    real_path = uripath._path
    monkeypatch.setattr(
        uripath, "_path", lambda text: target if text == "memory" else real_path(text)
    )

    results = {}
    for flag in ("--dry-run", "-v"):
        stdout, stderr = io.BytesIO(), io.StringIO()
        code = uripath.main(
            ["sync", flag, "--no-follow-symlinks", str(source), "memory"],
            stdout=stdout,
            stderr=stderr,
        )
        lines = stdout.getvalue().decode("utf-8").splitlines()
        assert not [x for x in lines if x.startswith(("would symlink", "symlink"))]
        results[flag] = (code, stderr.getvalue())

    assert results["--dry-run"] == results["-v"]
    code, message = results["--dry-run"]
    assert code == 1
    assert "NotImplementedError" in message and "MemPath" in message


@pytest.mark.parametrize(
    ("value", "named"),
    [
        ("http://user:s3cr3t@127.0.0.1:9/x", "the 'http' scheme"),
        ("ftp://user:s3cr3t@127.0.0.1:9/x", "the 'ftp' scheme"),
        ("sftp://user:s3cr3t@127.0.0.1:9/x", "the 'sftp' scheme"),
        ("dav://user:s3cr3t@127.0.0.1:9/x", "the 'dav' scheme"),
        ("s3://user:s3cr3t@bucket/key", "the 's3' scheme"),
        ("github://s3cr3t@host/owner/repo/file", "the 'github' scheme"),
        ("unknown-scheme://user:s3cr3t@host/x", "the 'unknown-scheme' scheme"),
    ],
)
def test_missing_extra_names_the_scheme_and_never_the_argument(
    value, named, without_uri_extra, capsys
):
    for argv in (["read", value], ["cp", value, "out"], ["sync", value, "out"]):
        stdout, stderr = io.BytesIO(), io.StringIO()
        assert uripath.main(argv, stdout=stdout, stderr=stderr) == 1
        assert stdout.getvalue() == b""
        assert stderr.getvalue() == (
            f"uripath: ImportError: {named} needs the 'uri' extra:"
            " pip install 'pathlib-next[uri]'\n"
        )
    assert "s3cr3t" not in "".join(capsys.readouterr())
