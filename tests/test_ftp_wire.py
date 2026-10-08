"""What `ftp:` sends and accepts on the wire: one `MLST` reply for a stat, a
`NOOP` only after idle time, a listing or a size fact that is malformed, a
refused `RENAME`, and the lifetime of a thread's connection. A scriptable
loopback server logs every command (tests/ftp_loopback.py)."""

import errno
import ftplib
import threading

import pytest

pytest.importorskip("uritools")

from pathlib_next.uri.schemes import ftp as ftp_mod
from pathlib_next.uri.schemes.ftp import FtpBackend, FtpPath

from ftp_loopback import LoopbackFtp
from waits import wait_until


def _mlsd(*lines):
    return b"".join(
        (line.encode("utf-8", "surrogateescape") if isinstance(line, str) else line)
        + b"\r\n"
        for line in lines
    )


@pytest.fixture
def make():
    """`make(**options)` -> (server, path factory); everything is closed and
    every cached connection to it dropped when the test ends."""
    servers = []

    def start(**options):
        server = LoopbackFtp(**options)
        servers.append(server)
        backend = FtpBackend(timeout=5)

        def path(text):
            return FtpPath(f"{server.url}{text}", backend=backend)

        return server, path

    yield start
    for server in servers:
        for key in list(ftp_mod._CACHED_CLIENTS.cache):
            if key[1].port == server.port:
                ftp_mod._CACHED_CLIENTS.discard(*key)
        server.close()


FILE = "type=file;size=5;modify=20240101000000;"
DIRS = {"/", "/d", "/d/sub"}


# --- a stat is one reply ----------------------------------------------------------------


def test_stat_of_a_file_is_one_mlst_on_a_warm_connection(make):
    server, path = make(
        files={"/d/f.txt": b"hello"}, dirs=DIRS, entries={"/d/f.txt": FILE}
    )
    target = path("/d/f.txt")
    target.stat()
    server.commands()
    st = target.stat()
    assert server.commands() == ["MLST"]
    assert (st.st_size, st.st_mtime, st.is_dir()) == (5, 1704067200, False)


def test_stat_of_a_directory_is_one_mlst(make):
    server, path = make(dirs=DIRS, entries={"/d": "type=dir;", "/d/sub": "type=dir;"})
    target = path("/d/sub")
    target.stat()
    server.commands()
    assert target.is_dir()
    assert server.commands() == ["MLST"]


def test_exists_of_a_missing_file_sends_four_commands_and_no_listing(make):
    server, path = make(dirs=DIRS, entries={})
    target = path("/d/nope")
    path("/d").exists()
    server.commands()
    assert not target.exists()
    assert server.commands() == ["MLST", "TYPE", "SIZE", "CWD"]


def test_a_burst_of_stats_sends_no_noop_and_a_pause_sends_one(make, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ftp_mod, "_now", lambda: clock[0])
    server, path = make(
        files={"/d/f.txt": b"hello"}, dirs=DIRS, entries={"/d/f.txt": FILE}
    )
    target = path("/d/f.txt")
    target.stat()
    server.commands()
    for _ in range(5):
        clock[0] += ftp_mod.IDLE_PROBE_SECONDS / 10
        target.stat()
    assert server.commands() == ["MLST"] * 5
    clock[0] += ftp_mod.IDLE_PROBE_SECONDS + 1
    target.stat()
    assert server.commands() == ["NOOP", "MLST"]
    target.stat()
    assert server.commands() == ["MLST"]


def test_a_connection_the_server_dropped_while_idle_is_replaced(make, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ftp_mod, "_now", lambda: clock[0])
    server, path = make(
        files={"/d/f.txt": b"hello"}, dirs=DIRS, entries={"/d/f.txt": FILE}
    )
    target = path("/d/f.txt")
    target.stat()
    # The server closes the session; the probe finds out before the command.
    (client,) = [
        c for k, c in ftp_mod._CACHED_CLIENTS.cache.items() if k[1].port == server.port
    ]
    client.sock.shutdown(2)
    clock[0] += ftp_mod.IDLE_PROBE_SECONDS + 1
    assert target.stat().st_size == 5
    assert server.total == 2


# --- a server that does not offer MLST, or answers it badly -------------------------------


@pytest.mark.parametrize(
    "features", [None, ["MLSD", "UTF8"]], ids=["no FEAT", "FEAT without MLST"]
)
def test_stat_without_mlst_reads_the_parents_listing_and_asks_feat_once(make, features):
    server, path = make(
        files={"/d/f.txt": b"hello"},
        dirs=DIRS,
        features=features,
        listings={
            "/d": _mlsd("type=cdir; .", "type=file;size=5;modify=20240101000000; f.txt")
        },
    )
    target = path("/d/f.txt")
    first = target.stat()
    commands = server.commands()
    assert commands.count("FEAT") == 1 and "MLST" not in commands and "MLSD" in commands
    again = target.stat()
    assert server.commands() == ["TYPE", "PASV", "MLSD"]
    assert (first.st_size, again.st_size, again.st_mtime) == (5, 5, 1704067200)


def test_an_mlst_that_answers_without_an_entry_is_not_asked_again(make):
    def no_entry(session, arg):
        session.send("250 nothing to say")
        return True

    server, path = make(
        files={"/d/f.txt": b"hello"},
        dirs=DIRS,
        overrides={"MLST": no_entry},
        listings={"/d": _mlsd("type=file;size=5; f.txt")},
    )
    target = path("/d/f.txt")
    assert target.stat().st_size == 5
    assert server.commands().count("MLST") == 1
    assert target.stat().st_size == 5
    assert "MLST" not in server.commands()


# --- a name the client cannot decode makes only its own directory unlistable -------------------


def _latin1_siblings():
    return _mlsd("type=file;size=5; plain.txt", b"type=file;size=1; caf\xe9.txt")


@pytest.mark.parametrize(
    "features", [("MLST type*;size*;", "MLSD"), None], ids=["MLST", "no MLST"]
)
def test_a_sibling_the_client_cannot_decode_does_not_break_a_stat(make, features):
    server, path = make(
        files={"/d/plain.txt": b"hello"},
        dirs=DIRS,
        features=features,
        entries={"/d/plain.txt": "type=file;size=5;"},
        listings={"/d": _latin1_siblings()},
    )
    target = path("/d/plain.txt")
    for _ in range(3):
        assert target.exists() and target.is_file()
        assert target.stat().st_size == 5
    with pytest.raises(OSError) as raised:
        list(path("/d").iterdir())
    assert raised.value.errno == errno.EILSEQ


def test_stats_beside_an_undecodable_name_with_mlst_use_one_connection(make):
    server, path = make(
        files={"/d/plain.txt": b"hello"},
        dirs=DIRS,
        entries={"/d/plain.txt": "type=file;size=5;"},
        listings={"/d": _latin1_siblings()},
    )
    target = path("/d/plain.txt")
    for _ in range(9):
        assert target.stat().st_size == 5
    assert server.total == 1


# --- a size fact that is not a size ----------------------------------------------------------


def test_a_size_fact_that_is_not_a_number_is_unknown_in_a_listing_and_a_stat(make):
    server, path = make(
        dirs=DIRS,
        entries={"/d/a": "type=file;size=abc;", "/d/e": "type=file;size=7;"},
        listings={
            "/d": _mlsd(
                "type=file;size=abc; a",
                "type=file;size=1.5; b",
                "type=file;size=-1; c",
                "type=file;size=" + "9" * 400 + "; d",
                "type=file;size=7; e",
                "type=file; f",
            )
        },
    )
    sizes = {child.name: child.stat().st_size for child in path("/d").iterdir()}
    assert sizes == {"a": 0, "b": 0, "c": 0, "d": 0, "e": 7, "f": 0}
    assert path("/d/a").stat().st_size == 0
    assert path("/d/e").stat().st_size == 7


# --- a rename the server refuses -------------------------------------------------------


def test_renaming_onto_an_existing_file_the_server_keeps_is_file_exists(make):
    server, path = make(files={"/a": b"A", "/b": b"B"}, dirs={"/"})
    with pytest.raises(FileExistsError) as raised:
        path("/a").rename("b")
    assert str(raised.value.filename).endswith("/b")
    assert "550" in str(raised.value)
    assert server.files == {"/a": b"A", "/b": b"B"}


def test_a_rename_refused_for_permission_stays_a_permission_error(make):
    def denied(session, arg):
        session.send("550 Permission denied.")
        return True

    server, path = make(
        files={"/a": b"A", "/b": b"B"}, dirs={"/"}, overrides={"RNTO": denied}
    )
    with pytest.raises(PermissionError):
        path("/a").rename("b")


def test_a_rename_refused_with_no_target_there_stays_a_permission_error(make):
    def refuse(session, arg):
        session.send("553 name not allowed")
        return True

    server, path = make(files={"/a": b"A"}, dirs={"/"}, overrides={"RNTO": refuse})
    with pytest.raises(PermissionError):
        path("/a").rename("zz")


def test_renaming_a_missing_file_is_not_found(make):
    server, path = make(files={}, dirs={"/"})
    with pytest.raises(FileNotFoundError):
        path("/a").rename("b")


def test_a_rename_to_a_free_name_lands_and_returns_the_new_path(make):
    server, path = make(files={"/a": b"A"}, dirs={"/"})
    moved = path("/a").rename("b")
    assert moved.path == "/b" and server.files == {"/b": b"A"}


# --- the lifetime of a connection ------------------------------------------------------


def test_the_connection_of_a_thread_that_ended_is_closed(make):
    server, path = make(
        files={"/d/f.txt": b"hello"}, dirs=DIRS, entries={"/d/f.txt": FILE}
    )

    def work():
        assert path("/d/f.txt").stat().st_size == 5

    threads = [threading.Thread(target=work) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert wait_until(lambda: not server.live), f"{len(server.live)} still open"
    assert server.total == 20
    path("/d/f.txt").stat()
    assert len(server.live) == 1


def test_a_connection_dropped_by_another_thread_mid_transfer_finishes_it_then_closes(
    make,
):
    release = threading.Event()
    started = threading.Event()

    server, path = make(
        files={"/big": b""},
        dirs={"/"},
        overrides={"RETR": lambda s, a: _slow(s, a, started, release)},
    )
    results = []

    def reader():
        results.append(path("/big").read_bytes())

    thread = threading.Thread(target=reader)
    thread.start()
    assert started.wait(10)
    (key,) = [k for k in ftp_mod._CACHED_CLIENTS.cache if k[1].port == server.port]
    ftp_mod._CACHED_CLIENTS.discard(*key)  # the cache overflowed in another thread
    assert len(server.live) == 1  # not closed under the running transfer
    release.set()
    thread.join(10)
    assert results == [b"x" * 1000]
    assert wait_until(lambda: not server.live)


def _slow(session, arg, started, release):
    # PASV was sent by the client before RETR: accept its data connection
    # only once the test lets the transfer go on.
    session.send("150 here it comes")
    started.set()
    release.wait(10)
    data = session.data()
    data.sendall(b"x" * 1000)
    data.close()
    session.send("226 done")
    return True


# --- errors say what the server said -------------------------------------------------------


def test_a_chmod_the_server_refuses_carries_its_reply_and_chains_to_it(make):
    def refuse(session, arg):
        session.send("550 Not enough privileges.")
        return True

    server, path = make(
        files={"/a": b"A"},
        dirs={"/"},
        entries={"/a": "type=file;"},
        overrides={"SITE": refuse},
    )
    with pytest.raises(NotImplementedError) as raised:
        path("/a").chmod(0o600)
    assert "550 Not enough privileges" in str(raised.value)
    assert isinstance(raised.value.__cause__, ftplib.error_perm)


def test_a_chmod_a_server_lacks_says_so_and_chains_to_the_reply(make):
    def lacks(session, arg):
        session.send("502 SITE CHMOD not implemented")
        return True

    server, path = make(files={"/a": b"A"}, dirs={"/"}, overrides={"SITE": lacks})
    with pytest.raises(NotImplementedError) as raised:
        path("/a").chmod(0o600)
    assert "not supported by this server" in str(raised.value)
    assert "502" in str(raised.value)
    assert isinstance(raised.value.__cause__, ftplib.error_perm)


def test_a_transient_reply_is_an_oserror_with_the_reply_in_it(make):
    def busy(session, arg):
        session.pasv and session.pasv.close()
        session.pasv = None
        session.send("425 Can't open data connection")
        return True

    server, path = make(files={"/a": b"A"}, dirs={"/"}, overrides={"RETR": busy})
    with pytest.raises(OSError) as raised:
        path("/a").read_bytes()
    assert raised.value.errno == errno.EAGAIN
    assert "425 Can't open data connection" in str(raised.value)
    assert isinstance(raised.value.__cause__, ftplib.error_temp)
