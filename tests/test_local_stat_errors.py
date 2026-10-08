"""What `LocalPath.exists()` and the `is_*()` predicates do when `stat()`
fails, and what the shipped header says about it."""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

import pathlib_next

HEADER = pathlib.Path(pathlib_next.__file__).with_name("AGENTS.md")

PREDICATES = [
    "is_dir",
    "is_file",
    "is_fifo",
    "is_socket",
    "is_block_device",
    "is_char_device",
]


class Denied(pathlib_next.LocalPath):
    __slots__ = ()

    def stat(self, *, follow_symlinks=True):
        raise PermissionError(13, "denied")


def test_exists_is_false_for_any_error_on_every_version(tmp_path):
    assert Denied(tmp_path / "f").exists() is False


@pytest.mark.skipif(
    sys.version_info >= (3, 13), reason="3.13 stdlib no longer asks stat()"
)
@pytest.mark.parametrize("name", PREDICATES)
def test_the_predicates_before_313_raise_what_stdlib_raises(tmp_path, name):
    with pytest.raises(PermissionError):
        getattr(Denied(tmp_path / "f"), name)()


def test_the_header_claims_no_predicate_beyond_what_stdlib_does():
    text = " ".join(HEADER.read_text(encoding="utf-8").split())
    claims = [
        text[max(0, m.start() - 160) : m.end()]
        for m in re.finditer(r"on every Python version", text)
    ]
    assert claims
    assert not [claim for claim in claims if "is_*()" in claim]
    assert any("exists()" in claim for claim in claims)
