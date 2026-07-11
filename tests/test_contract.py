"""Filesystem contract shared by every Path implementation.

This is the reusable "one test class, many backends" pattern the project
wants third-party implementers (custom Path subclasses like MemPath, or
UriPath schemes) to be able to run against their own implementation -- see
AGENTS.md's Track A/Track B. `PathContract` (the mixin) is exported as
`pathlib_next.testing.PathContract`; `TestContract` here wires it to our
three reference backends via a parametrized `root` fixture.
"""
import pytest

import pathlib_next
from pathlib_next.mempath import MemPath
from pathlib_next.testing import PathContract
from pathlib_next.uri.schemes.file import FileUri

BACKENDS = ["local", "mem", "fileuri"]


class TestContract(PathContract):
    @pytest.fixture(params=BACKENDS)
    def root(self, request, tmp_path):
        if request.param == "local":
            return pathlib_next.LocalPath(tmp_path)
        if request.param == "mem":
            return MemPath("/")
        if request.param == "fileuri":
            return FileUri(tmp_path.as_uri())
        raise AssertionError(request.param)
