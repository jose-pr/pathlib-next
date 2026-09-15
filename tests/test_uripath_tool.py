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
