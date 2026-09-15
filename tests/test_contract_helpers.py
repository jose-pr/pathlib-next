"""The shipped `pathlib_next.testing` contracts themselves: the documented
example runs verbatim, capability opt-outs skip instead of passing, and the
negative-path tests fail on a backend that breaks pathlib's rules.

Kept free of `pathlib_next.uri` imports so it also runs without the `uri`
extra.
"""

import os
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest

import pathlib_next
from pathlib_next import testing
from pathlib_next.mempath import MemPath
from pathlib_next.testing import (
    FIXTURE_TREE,
    PathContract,
    ReadPathContract,
    populate_fixture_tree,
)

REPO = pathlib.Path(__file__).resolve().parent.parent


def _docstring_example():
    doc = testing.__doc__
    block = doc.split("Example::\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line.strip() and not line.startswith("    "):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines)).strip() + "\n"


def _guide_example():
    guide = (REPO / "docs" / "guides" / "extending.md").read_text(encoding="utf-8")
    section = guide.split("### Example: Running the full contract", 1)[1]
    match = re.search(r"```python\n(.*?)```", section, re.S)
    return match.group(1)


def _contract_test_names(cls):
    return sorted(name for name in dir(cls) if name.startswith("test_"))


def test_docstring_and_guide_show_the_same_example():
    assert _docstring_example() == _guide_example()


@pytest.mark.parametrize("source", ["docstring", "guide"])
def test_documented_example_passes_verbatim(source, tmp_path):
    code = _docstring_example() if source == "docstring" else _guide_example()
    (tmp_path / "test_documented_example.py").write_text(code, encoding="utf-8")
    # An empty ini pins rootdir here: the repo's own pytest config (and this
    # conftest) must not help the example pass.
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO / "src"), env.get("PYTHONPATH")])
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    expected = len(_contract_test_names(PathContract))
    assert result.returncode == 0, result.stdout + result.stderr
    summary = result.stdout.strip().splitlines()[-1]
    assert re.match(rf"{expected} passed in ", summary), result.stdout


def test_populate_fixture_tree_builds_the_standard_tree(tmp_path):
    root = MemPath("/")
    assert populate_fixture_tree(root) is root
    for name, content in FIXTURE_TREE.items():
        path = root.joinpath(*name.split("/"))
        if content is None:
            assert path.is_dir(), name
        else:
            assert path.read_text() == content, name

    populate_fixture_tree(tmp_path)  # a stdlib pathlib.Path works too
    found = {
        p.relative_to(tmp_path).as_posix(): (None if p.is_dir() else p.read_text())
        for p in tmp_path.rglob("*")
    }
    assert found == FIXTURE_TREE


# --- a contract must fail (never pass) on a broken backend ------------------


class _NoListing(MemPath):
    def iterdir(self):
        raise NotImplementedError("listing never implemented")


class _ListingAlwaysNotADirectory(MemPath):
    def iterdir(self):
        raise NotADirectoryError(self)


class _SilentMissingParents(MemPath):
    def _mkdir(self, mode):
        if self.parent != self and not self.parent.exists():
            self.parent.mkdir(parents=True)
        super()._mkdir(mode)


class _RmdirFileExists(MemPath):
    def rmdir(self):
        if self.is_dir() and any(True for _ in self.iterdir()):
            raise FileExistsError(self)
        super().rmdir()


class _ListsFilesAsEmpty(MemPath):
    def iterdir(self):
        if self.is_file():
            return iter(())
        return super().iterdir()


@pytest.mark.parametrize("backend_cls", [_NoListing, _ListingAlwaysNotADirectory])
def test_iterdir_contract_fails_on_broken_listing(backend_cls):
    root = populate_fixture_tree(backend_cls("/"))
    with pytest.raises((NotImplementedError, NotADirectoryError)):
        ReadPathContract().test_iterdir_lists_children(root)


def test_capability_opt_out_skips_instead_of_passing():
    class Contract(ReadPathContract):
        supports_listing = False

    root = populate_fixture_tree(_NoListing("/"))
    for name in ("test_iterdir_lists_children", "test_glob", "test_walk"):
        with pytest.raises(pytest.skip.Exception):
            getattr(Contract(), name)(root)


@pytest.mark.parametrize(
    "backend_cls, test_name, failure",
    [
        (
            _SilentMissingParents,
            "test_mkdir_missing_parent_raises_file_not_found",
            pytest.fail.Exception,  # DID NOT RAISE
        ),
        # FileExistsError is an OSError, but not ENOTEMPTY.
        (_RmdirFileExists, "test_rmdir_requires_empty", AssertionError),
        (
            _ListsFilesAsEmpty,
            "test_iterdir_file_raises_not_a_directory",
            pytest.fail.Exception,
        ),
    ],
)
def test_negative_path_contract_catches_violation(backend_cls, test_name, failure):
    root = populate_fixture_tree(backend_cls("/"))
    with pytest.raises(failure):
        getattr(PathContract(), test_name)(root)


def test_negative_path_contract_passes_on_localpath(tmp_path):
    # The same calls as above, on a correct implementation.
    contract = PathContract()
    for name in _contract_test_names(PathContract):
        root = tmp_path / name
        root.mkdir()
        getattr(contract, name)(populate_fixture_tree(pathlib_next.LocalPath(root)))
