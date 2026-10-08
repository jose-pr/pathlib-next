"""Run in a child pytest by `test_hermetic.py`: a test that writes the real
process environment must be reported as an error at its teardown."""

import os


def test_a_clean_test():
    assert "PATHLIB_NEXT_LEAKED" not in os.environ


def test_writes_the_environment():
    os.environ["PATHLIB_NEXT_LEAKED"] = "yes"
