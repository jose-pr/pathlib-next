import io

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
