"""The session-end census sees what is alive, and reports it only when asked."""

import asyncio
import os
import pathlib
import socket
import subprocess
import sys
import threading

import pytest

import census

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_the_census_sees_a_socket_a_loop_a_process_and_a_thread_until_they_end():
    baseline = len(census.take_census().items)
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="census-probe")
    left, right = socket.socketpair()
    loop = asyncio.new_event_loop()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    thread.start()
    try:
        found = census.take_census()
        assert len(found.items) - baseline >= 5  # both ends of the pair count
        kinds = {item.kind for item in found.items}
        assert {"socket", "unclosed loop", "running subprocess", "thread"} <= kinds
        assert any(item.description == f"pid {child.pid}" for item in found.items)
        assert any(item.description == "census-probe" for item in found.items)
    finally:
        child.kill()
        child.wait(timeout=30)
        release.set()
        thread.join(timeout=30)
        left.close()
        right.close()
        loop.close()
    assert len(census.take_census().items) == baseline


def test_a_listening_socket_is_named_as_one():
    listener = socket.create_server(("127.0.0.1", 0))
    try:
        try:
            listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
            expected = "listening socket"
        except OSError:
            # A platform that will not say (macOS): it is still counted.
            expected = "socket"
        kinds = [item.kind for item in census.take_census().items]
    finally:
        listener.close()
    assert expected in kinds


def _run(*args):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO / "src"), env.get("PYTHONPATH")])
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(REPO / "tests" / "probes" / "probe_leaks_a_socket.py"),
            *args,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_without_the_flag_nothing_is_listed():
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "leak census" not in result.stdout


def test_the_flag_lists_what_is_alive_and_still_passes():
    result = _run("--leak-census")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "leak census" in result.stdout
    assert "socket: <socket.socket" in result.stdout


def test_the_fail_form_exits_non_zero():
    result = _run("--leak-census=fail")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "leak census" in result.stdout
    assert "1 passed" in result.stdout


def test_a_run_that_leaks_nothing_says_so():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(REPO / "tests" / "test_census.py"),
            "-k",
            "listening_socket",
            "--leak-census=fail",
        ],
        cwd=REPO,
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                filter(None, [str(REPO / "src"), os.environ.get("PYTHONPATH")])
            ),
        ),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing is still alive" in result.stdout
