"""The examples that need a remote host do nothing, and exit 0, until the
environment variables they document are set: running every example in a
checkout never leaves the machine.
"""

import os
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("uritools")

REPO = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
LOCAL = {"local_and_mem.py", "data_and_archive.py"}

# Runs the example as `__main__` after making any connection or name lookup
# end the process with a message the test can see.
RUNNER = """
import runpy, socket, sys

def refuse(*args, **kwargs):
    raise SystemExit("the example opened a connection or looked up a name")

socket.socket.connect = refuse
socket.socket.connect_ex = refuse
socket.getaddrinfo = refuse
runpy.run_path(sys.argv[1], run_name="__main__")
"""

NETWORKED = sorted(p.name for p in EXAMPLES.glob("*.py") if p.name not in LOCAL)


def test_every_example_is_local_or_listed_as_networked():
    assert NETWORKED
    for name in NETWORKED:
        text = (EXAMPLES / name).read_text(encoding="utf-8")
        assert "os.environ" in text, f"{name} reads no variable, so it is local"


@pytest.mark.parametrize("name", NETWORKED)
def test_a_networked_example_skips_without_its_variables(name, tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.endswith("_URL") and "_EXAMPLE_" not in key
    }
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO / "src"), env.get("PYTHONPATH")])
    )
    result = subprocess.run(
        [sys.executable, "-c", RUNNER, str(EXAMPLES / name)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "skipping" in output
    assert "opened a connection" not in output
