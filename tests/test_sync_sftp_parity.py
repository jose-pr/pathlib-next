"""PathSyncer and SftpPath parity against the loopback SFTP server.

Regressions for the 2026-09-15 deep review, wave 5 group G6: sync onto an
SFTP target keeps the previous version of a file until the new one is
complete, dry runs and remove_missing events on an SFTP tree, and the SFTP
medium findings (rename over an existing file, unlink of a dangling link,
readlink, asyncssh file-object contract, asyncssh recursive copy
guards and `ignore_error` bools, exit with a file left open, and the
paramiko check-file probe). Every test asserts what the server's filesystem
(`fixture_tree`, the server root) holds afterwards, or what was sent.
"""

import errno
import hashlib
import io
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

pytest.importorskip("asyncssh")

import pathlib_next  # noqa: E402
from pathlib_next.mempath import MemPath  # noqa: E402
from pathlib_next.uri.schemes.sftp import SftpPath  # noqa: E402
from pathlib_next.uri.schemes.sftp import _asyncssh as backend_mod  # noqa: E402
from pathlib_next.utils.sync import PathSyncer, SyncEvent  # noqa: E402

try:
    import paramiko
except ImportError:  # asyncssh-only install
    paramiko = None


def _size(entry):
    return entry.stat.st_size


def _make_backend(kind):
    if kind == "paramiko":
        if paramiko is None:
            pytest.skip("paramiko not installed")
        from pathlib_next.uri.schemes.sftp import SftpBackend

        return SftpBackend({"allow_agent": False, "look_for_keys": False})
    return backend_mod.AsyncsshSftpBackend()


def _close(backend, path):
    if paramiko is not None and not isinstance(
        backend, backend_mod.AsyncsshSftpBackend
    ):
        from pathlib_next.uri.schemes.sftp._paramiko import _CACHED_CLIENTS

        try:
            backend.client(path.source).sock.get_transport().close()
        except Exception:
            pass
        _CACHED_CLIENTS.invalidate(backend, path.source, threading.get_ident())
    else:
        backend.close()


@pytest.fixture(params=["paramiko", "asyncssh"])
def remote(request, sftp_server, fixture_tree):
    """(SftpPath at the server root, local path of that root)."""
    backend = _make_backend(request.param)
    root = SftpPath(sftp_server, backend=backend)
    try:
        yield root, fixture_tree
    finally:
        _close(backend, root)


@pytest.fixture
def aremote(sftp_server, fixture_tree):
    backend = _make_backend("asyncssh")
    root = SftpPath(sftp_server, backend=backend)
    try:
        yield root, fixture_tree
    finally:
        _close(backend, root)


def _symlink(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlink unavailable: {error}")


def _names(path):
    return sorted(p.name for p in path.iterdir())


# --- PathSyncer onto an SFTP target ------------------------------------------


class _ResetStream(io.RawIOBase):
    def __init__(self, data):
        self._data = data
        self._reads = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        self._reads += 1
        if self._reads > 1:
            raise ConnectionResetError(errno.ECONNRESET, "connection reset")
        chunk = self._data[:4]
        buffer[: len(chunk)] = chunk
        return len(chunk)


class _FlakyMemPath(MemPath):
    def _open(self, mode="r", buffering=-1):
        handle = super()._open(mode, buffering)
        if mode == "r" and self.name == "data.bin":
            return _ResetStream(handle.read())
        return handle


def test_sync_failed_copy_keeps_previous_remote_version(remote):
    root, local = remote
    (local / "dst").mkdir()
    (local / "dst" / "data.bin").write_bytes(b"previous good version")
    source = _FlakyMemPath("/src")
    source.mkdir()
    (source / "data.bin").write_bytes(b"NEW! content of another size")

    with pytest.raises(ConnectionResetError):
        PathSyncer(_size).sync(source, root / "dst")

    assert (local / "dst" / "data.bin").read_bytes() == b"previous good version"
    assert _names(local / "dst") == ["data.bin"]


def test_sync_replaces_changed_remote_file(remote):
    root, local = remote
    (local / "dst").mkdir()
    (local / "dst" / "a.txt").write_text("old")
    source = MemPath("/src")
    source.mkdir()
    (source / "a.txt").write_text("new, longer content")

    PathSyncer(_size).sync(source, root / "dst")

    assert (local / "dst" / "a.txt").read_text() == "new, longer content"
    assert _names(local / "dst") == ["a.txt"]


def test_sync_dry_run_onto_remote_new_subdirectory_changes_nothing(remote):
    root, local = remote
    (local / "dst").mkdir()
    (local / "dst" / "stale.txt").write_text("stale")
    source = MemPath("/src")
    (source / "newsub" / "deep").mkdir(parents=True)
    (source / "newsub" / "deep" / "f.txt").write_text("f")
    events = []

    def hook(source_entry, target_entry, event, dry_run):
        if event is SyncEvent.RemovedMissing:
            events.append(target_entry.path.name)

    PathSyncer(_size, remove_missing=True, hook=hook).sync(
        source, root / "dst", dry_run=True
    )

    assert _names(local / "dst") == ["stale.txt"]
    assert events == ["stale.txt"]


# --- SftpPath: rename, unlink, readlink ----------------------------------------


def test_rename_over_existing_file_replaces_it(remote):
    # sftp-rename-over-existing-parity: POSIX rename semantics through
    # posix-rename@openssh.com (the loopback server advertises it).
    root, local = remote
    (local / "old.txt").write_text("new content")
    (local / "new.txt").write_text("stale content")

    (root / "old.txt").rename(root / "new.txt")

    assert (local / "new.txt").read_text() == "new content"
    assert not (local / "old.txt").exists()


def test_rename_to_missing_target_still_works(remote):
    root, local = remote
    (local / "old.txt").write_text("content")

    (root / "old.txt").rename("fresh.txt")

    assert (local / "fresh.txt").read_text() == "content"
    assert not (local / "old.txt").exists()


def test_unlink_missing_ok_removes_dangling_symlink(remote):
    # sftp-unlink-missing-ok-dangling-symlink
    root, local = remote
    _symlink("release-1", local / "current")
    link = root / "current"
    assert link.is_symlink() and not link.exists()

    link.unlink(missing_ok=True)

    assert not os.path.lexists(local / "current")
    (root / "gone").unlink(missing_ok=True)
    with pytest.raises(FileNotFoundError):
        (root / "gone").unlink()


def test_symlink_force_repoints_dangling_link(remote):
    root, local = remote
    _symlink("release-1", local / "current")

    (root / "current").symlink_to("release-2", force=True)

    assert os.readlink(local / "current") == "release-2"


def test_readlink_absolute_target_keeps_the_server(remote):
    # A relative target cannot be read back through this chrooted asyncssh
    # server (it resolves links against its own cwd), so the relative case
    # is covered with a fake client in tests/test_sftp.py.
    root, local = remote
    (local / "releases" / "42").mkdir(parents=True)
    _symlink(str(local / "releases" / "42"), local / "current")

    link = (root / "current").readlink()

    assert link.path == "/releases/42"
    assert link.source == root.source
    assert str(link).endswith("/releases/42")


# --- asyncssh file objects -------------------------------------------------------


def test_asyncssh_binary_readline_is_buffered(aremote, monkeypatch):
    # sftp-syncfile-io-contract-gaps: readline() was one round trip per byte.
    root, local = aremote
    (local / "big.log").write_bytes(b"x" * 1024 + b"\n" + b"tail\n")
    calls = []
    real_run = backend_mod._run

    def counting_run(coro, *args, **kwargs):
        calls.append(coro)
        return real_run(coro, *args, **kwargs)

    with (root / "big.log").open("rb") as handle:
        monkeypatch.setattr(backend_mod, "_run", counting_run)
        assert handle.readline() == b"x" * 1024 + b"\n"
        assert handle.readline() == b"tail\n"
        monkeypatch.setattr(backend_mod, "_run", real_run)

    assert len(calls) < 5


def test_asyncssh_file_modes_are_reported(aremote):
    root, local = aremote
    (local / "f.bin").write_bytes(b"data")

    with (root / "f.bin").open("rb") as handle:
        assert handle.readable() and not handle.writable()
        with pytest.raises(io.UnsupportedOperation):
            handle.write(b"nope")
    with (root / "g.bin").open("wb") as handle:
        assert handle.writable() and not handle.readable()
        handle.write(b"written")
    assert (local / "g.bin").read_bytes() == b"written"
    with (root / "f.bin").open("r+b") as handle:
        assert handle.readable() and handle.writable()
    assert (local / "f.bin").read_bytes() == b"data"


def test_asyncssh_file_readinto_and_read_bytes(aremote):
    root, local = aremote
    payload = os.urandom(300 * 1024)
    (local / "blob.bin").write_bytes(payload)

    assert (root / "blob.bin").read_bytes() == payload
    with (root / "blob.bin").open("rb", buffering=0) as raw:
        buffer = bytearray(10)
        assert raw.readinto(buffer) == 10
        assert bytes(buffer) == payload[:10]
    if hasattr(hashlib, "file_digest"):
        with (root / "blob.bin").open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        assert digest == hashlib.sha256(payload).hexdigest()


def test_asyncssh_close_errors_are_translated_and_close_the_object(monkeypatch):
    class _FailingAsyncFile:
        async def close(self):
            import asyncssh

            raise asyncssh.SFTPFailure("close failed")

    raw = backend_mod._SyncSftpFile(_FailingAsyncFile(), timeout=5, mode="r")
    with pytest.raises(OSError):
        raw.close()
    assert raw.closed


def test_asyncssh_file_left_open_does_not_hang_interpreter_exit(
    aremote, sftp_server, tmp_path
):
    # sftp-exit-hang-unclosed-file: a module-global handle finalized at
    # shutdown used to wait the full 60 s request timeout.
    root, local = aremote
    (local / "log.txt").write_text("line\n")
    script = tmp_path / "leak_handle.py"
    script.write_text(textwrap.dedent(f"""
            from pathlib_next.uri.schemes.sftp import SftpPath
            from pathlib_next.uri.schemes.sftp._asyncssh import AsyncsshSftpBackend

            path = SftpPath({(sftp_server + "log.txt")!r}, backend=AsyncsshSftpBackend())
            f = path.open("rb")
            print(f.readline().decode().strip())
            """))

    env = os.environ.copy()
    # The child imports the same pathlib_next this test runs against.
    package_root = os.path.dirname(os.path.dirname(pathlib_next.__file__))
    env["PYTHONPATH"] = os.pathsep.join(
        [package_root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=50,
        env=env,
    )
    elapsed = time.monotonic() - started

    assert result.stdout.strip() == "line", result.stderr
    assert elapsed < 30, result.stderr
    assert "TimeoutError" not in result.stderr


# --- asyncssh recursive copy -----------------------------------------------------


def _copy_tree(local):
    (local / "src" / "sub").mkdir(parents=True)
    (local / "src" / "1.txt").write_text("one")
    (local / "src" / "sub" / "2.txt").write_text("two")


@pytest.mark.parametrize("ignore_error", [True, False])
def test_asyncssh_recursive_copy_accepts_bool_ignore_error(aremote, ignore_error):
    # sftp-concurrent-copy-bool-ignore-error: a bool was *called*.
    root, local = aremote
    _copy_tree(local)
    (local / "dst" / "1.txt").mkdir(parents=True)  # forces a child error

    if ignore_error:
        (root / "src").copy(
            root / "dst", recursive=True, overwrite=True, ignore_error=True
        )
        assert (local / "dst" / "sub" / "2.txt").read_text() == "two"
    else:
        with pytest.raises(IsADirectoryError):
            (root / "src").copy(
                root / "dst", recursive=True, overwrite=True, ignore_error=False
            )


def test_asyncssh_recursive_copy_callable_ignore_error_is_notified(aremote):
    root, local = aremote
    _copy_tree(local)
    (local / "dst" / "1.txt").mkdir(parents=True)
    errors = []

    (root / "src").copy(
        root / "dst", recursive=True, overwrite=True, ignore_error=errors.append
    )

    assert len(errors) == 1 and isinstance(errors[0], IsADirectoryError)
    assert (local / "dst" / "sub" / "2.txt").read_text() == "two"


def test_asyncssh_recursive_copy_into_own_subtree_raises_einval(aremote):
    root, local = aremote
    _copy_tree(local)

    with pytest.raises(OSError) as excinfo:
        (root / "src").copy(root / "src" / "sub" / "inner", recursive=True)

    assert excinfo.value.errno == errno.EINVAL
    assert not (local / "src" / "sub" / "inner").exists()
    assert _names(local / "src") == ["1.txt", "sub"]


def test_asyncssh_recursive_copy_keeps_symlinks_as_links(aremote):
    root, local = aremote
    _copy_tree(local)
    # Absolute link targets: this chrooted server cannot read back relative
    # ones (see test_readlink_absolute_target_keeps_the_server).
    _symlink(str(local / "src" / "1.txt"), local / "src" / "link.txt")
    _symlink(str(local / "src" / "sub"), local / "src" / "dirlink")

    (root / "src").copy(root / "dst", recursive=True, follow_symlinks=False)

    assert os.path.islink(local / "dst" / "link.txt")
    assert os.path.islink(local / "dst" / "dirlink")
    assert os.path.samefile(
        os.readlink(local / "dst" / "link.txt"), local / "src" / "1.txt"
    )
    assert os.path.samefile(
        os.readlink(local / "dst" / "dirlink"), local / "src" / "sub"
    )
    assert (local / "dst" / "sub" / "2.txt").read_text() == "two"


def test_asyncssh_copy_of_a_directory_link_without_following_copies_the_link(
    aremote,
):
    root, local = aremote
    _copy_tree(local)
    _symlink(str(local / "src"), local / "srclink")

    (root / "srclink").copy(root / "copied", recursive=True, follow_symlinks=False)

    assert os.path.islink(local / "copied")
    assert os.path.samefile(os.readlink(local / "copied"), local / "src")


# --- paramiko check-file probe -----------------------------------------------


def test_paramiko_checksum_refusal_costs_one_request_per_connection(
    sftp_server, fixture_tree, monkeypatch
):
    # sftp-check-file-openssh-extension-does-not-exist: a server without the
    # extension (this one, like OpenSSH) is asked once, not once per file.
    if paramiko is None:
        pytest.skip("paramiko not installed")
    backend = _make_backend("paramiko")
    root = SftpPath(sftp_server, backend=backend)
    try:
        requests = []
        real_request = paramiko.SFTPClient._request

        def counting_request(self, t, *args):
            if t == paramiko.sftp.CMD_EXTENDED:
                requests.append(args[0])
            return real_request(self, t, *args)

        monkeypatch.setattr(paramiko.SFTPClient, "_request", counting_request)
        (fixture_tree / "x.txt").write_text("x")
        (fixture_tree / "y.txt").write_text("y")

        for name in ("a.txt", "x.txt", "y.txt"):
            with pytest.raises(NotImplementedError):
                (root / name).checksum()
        assert (root / "a.txt").supported_checksums() == frozenset()

        assert requests == ["check-file-handle"]
    finally:
        _close(backend, root)


def test_sync_sftp_to_sftp_without_native_checksum_still_detects_changes(remote):
    root, local = remote
    (local / "s").mkdir()
    (local / "d").mkdir()
    (local / "s" / "cfg.json").write_text('{"v": 2}')
    (local / "d" / "cfg.json").write_text('{"v": 1}')

    PathSyncer().sync(root / "s", root / "d")

    assert (local / "d" / "cfg.json").read_text() == '{"v": 2}'
    assert _names(local / "d") == ["cfg.json"]


def test_sync_leaves_identical_remote_symlink_alone(remote, monkeypatch):
    # sync-identical-symlink-recreated, through SftpPath's `.path` link text
    # on both sides (absolute targets: this chrooted server cannot read a
    # relative one back, see test_readlink_absolute_target_keeps_the_server).
    root, local = remote
    (local / "releases" / "42").mkdir(parents=True)
    (local / "src").mkdir()
    _symlink(str(local / "releases" / "42"), local / "src" / "current")
    syncer = PathSyncer(_size, follow_symlinks=False)
    syncer.sync(root / "src", root / "dst")
    assert (root / "dst" / "current").readlink().path == "/releases/42"

    created = []
    monkeypatch.setattr(
        SftpPath, "_symlink_to", lambda self, *args: created.append(self)
    )
    events = []
    syncer._hook = lambda s, t, event, dry_run: events.append(event)
    syncer.sync(root / "src", root / "dst")

    assert created == []
    assert SyncEvent.Symlink not in events
    assert os.path.islink(local / "dst" / "current")
