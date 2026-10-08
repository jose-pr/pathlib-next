"""The shipped `pathlib_next.testing` contracts themselves: the documented
example runs verbatim, capability opt-outs skip instead of passing, and the
negative-path tests fail on a backend that breaks pathlib's rules.

Kept free of `pathlib_next.uri` imports so it also runs without the `uri`
extra.
"""

import errno
import os
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest

import pathlib_next
from pathlib_next import testing
from pathlib_next.mempath import MemBytesIO, MemPath, _MemReader
from pathlib_next.testing import (
    FIXTURE_TREE,
    PathContract,
    ReadPathContract,
    populate_fixture_tree,
)
from pathlib_next.utils.stat import FileStat

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
    # The one test an unmodified local backend may legitimately skip: a
    # Windows file system cannot store a name holding "?".
    assert re.match(rf"{expected - 1} passed, 1 skipped in ", summary), result.stdout
    assert "supports_question_mark_names is False" in result.stdout, result.stdout


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
    skipped = []
    for name in _contract_test_names(PathContract):
        root = tmp_path / name
        root.mkdir()
        try:
            getattr(contract, name)(populate_fixture_tree(pathlib_next.LocalPath(root)))
        except pytest.skip.Exception:
            skipped.append(name)
    assert skipped == [
        "test_names_with_a_question_mark_are_stored_and_listed_as_written"
    ]


# --- the tests for stored bytes, names, metadata and rename ---------------------
#
# Each backend below is a MemPath with one defect in what it stores or reports;
# the named contract test must fail on it.


class _StatSizeIsOne(MemPath):
    def stat(self, *, follow_symlinks=True):
        st = super().stat(follow_symlinks=follow_symlinks)
        return st if st.is_dir() else FileStat(st_size=1, st_mtime=st.st_mtime)


class _StatMtimeIsText(MemPath):
    def stat(self, *, follow_symlinks=True):
        st = super().stat(follow_symlinks=follow_symlinks)
        return FileStat(is_dir=st.is_dir(), st_size=st.st_size, st_mtime="yesterday")


class _StatCallsFilesDirectories(MemPath):
    def stat(self, *, follow_symlinks=True):
        st = super().stat(follow_symlinks=follow_symlinks)
        return FileStat(is_dir=True, st_size=st.st_size, st_mtime=st.st_mtime)


class _CrLfWriter(MemBytesIO):
    def write(self, data):
        super().write(bytes(data).replace(b"\r\n", b"\n"))
        return len(data)


class _WritesLoseCarriageReturns(MemPath):
    def _open(self, mode="r", buffering=-1):
        handle = super()._open(mode, buffering)
        if mode != "r":
            handle.__class__ = _CrLfWriter
        return handle


class _ReadsKeepSevenBits(MemPath):
    def _open(self, mode="r", buffering=-1):
        handle = super()._open(mode, buffering)
        if mode == "r":
            return _MemReader(bytes(b if b < 0x80 else 0x3F for b in handle.getvalue()))
        return handle


class _ReadsStopAt64KiB(MemPath):
    def _open(self, mode="r", buffering=-1):
        handle = super()._open(mode, buffering)
        if mode == "r":
            return _MemReader(handle.getvalue()[:65536])
        return handle


class _StoresEightKiB(MemPath):
    def _open(self, mode="r", buffering=-1):
        handle = super()._open(mode, buffering)
        if mode == "r":
            return handle

        class Capped(type(handle)):
            def _publish(self):
                super()._publish()
                del self._bytes[8192:]

        handle.__class__ = Capped
        return handle


class _ReadsIgnoreTheSize(MemPath):
    def _open(self, mode="r", buffering=-1):
        handle = super()._open(mode, buffering)
        if mode != "r":
            return handle

        class WholeReader(_MemReader):
            def read(self, size=-1):
                return super().read(-1)

        return WholeReader(handle.getvalue())


class _StatBelowAFileRaisesTypeError(MemPath):
    def stat(self, *, follow_symlinks=True):
        try:
            return super().stat(follow_symlinks=follow_symlinks)
        except NotADirectoryError:
            raise TypeError("argument of type 'bytearray' is not iterable")


class _ListingMetadataIsZero(MemPath):
    def _scandir(self):
        for child in self.iterdir():
            yield child.name, FileStat(is_dir=child.is_dir(), st_size=0, st_mtime=12345)


class _RenameReplacesAnything(MemPath):
    def rename(self, target):
        target = target if isinstance(target, MemPath) else self.with_segments(target)
        parent, name = self._parent_container()
        if name not in parent:
            raise FileNotFoundError(errno.ENOENT, "no such file", str(self))
        target_parent, target_name = target._parent_container()
        target_parent[target_name] = parent.pop(name)
        return target


class _StoredNamesAreCutAndDecoded(MemPath):
    def _parent_container(self):
        from urllib.parse import unquote

        parent, name = super()._parent_container()
        return parent, unquote(name.split("?")[0].split("#")[0])


class _StoredNamesAreFolded(MemPath):
    def _parent_container(self):
        parent, name = super()._parent_container()
        return parent, name.lower()


class _NoSpacesInNames(MemPath):
    def _parent_container(self):
        parent, name = super()._parent_container()
        if " " in name:
            raise FileNotFoundError(errno.ENOENT, "no such file", str(self))
        return parent, name


class _MemContract(PathContract):
    supports_rename = False


class _MemRenameContract(PathContract):
    pass


_EVERY_BYTE = "test_every_byte_value_survives_a_multi_chunk_round_trip"
_NAMES = "test_names_with_url_characters_are_stored_and_listed_as_written"
_QUESTION_NAMES = "test_names_with_a_question_mark_are_stored_and_listed_as_written"


@pytest.mark.parametrize(
    "backend_cls, contract, test_name",
    [
        (_StatSizeIsOne, _MemContract, "test_stat"),
        (_StatMtimeIsText, _MemContract, "test_stat"),
        (
            _StatCallsFilesDirectories,
            _MemContract,
            "test_stat_tells_a_directory_from_a_file",
        ),
        (_WritesLoseCarriageReturns, _MemContract, _EVERY_BYTE),
        (_ReadsKeepSevenBits, _MemContract, _EVERY_BYTE),
        (_ReadsStopAt64KiB, _MemContract, _EVERY_BYTE),
        (_StoresEightKiB, _MemContract, _EVERY_BYTE),
        (
            _ReadsIgnoreTheSize,
            _MemContract,
            "test_partial_reads_continue_where_the_last_stopped",
        ),
        (
            _StatBelowAFileRaisesTypeError,
            _MemContract,
            "test_nothing_exists_below_a_file",
        ),
        (
            _ListingMetadataIsZero,
            _MemContract,
            "test_listing_metadata_agrees_with_stat",
        ),
        (
            _RenameReplacesAnything,
            _MemRenameContract,
            "test_rename_onto_a_non_empty_directory_raises_and_keeps_both",
        ),
        (_StoredNamesAreCutAndDecoded, _MemContract, _NAMES),
        (_StoredNamesAreFolded, _MemContract, _NAMES),
        (_NoSpacesInNames, _MemContract, _NAMES),
    ],
)
def test_contract_fails_on_a_backend_that_corrupts_or_misreports(
    backend_cls, contract, test_name
):
    root = populate_fixture_tree(backend_cls("/"))
    with pytest.raises((Exception, pytest.fail.Exception)) as failure:
        getattr(contract(), test_name)(root)
    assert not isinstance(failure.value, pytest.skip.Exception)


@pytest.mark.parametrize(
    "test_name",
    [
        _EVERY_BYTE,
        _NAMES,
        "test_partial_reads_continue_where_the_last_stopped",
        "test_nothing_exists_below_a_file",
        "test_listing_metadata_agrees_with_stat",
        "test_stat",
    ],
)
def test_contract_passes_the_new_tests_on_a_correct_backend(test_name):
    root = populate_fixture_tree(MemPath("/"))
    getattr(_MemContract(), test_name)(root)


def test_the_question_mark_names_are_opt_in_and_run_when_enabled():
    class Contract(_MemContract):
        supports_question_mark_names = True

    root = populate_fixture_tree(MemPath("/"))
    with pytest.raises(pytest.skip.Exception):
        getattr(_MemContract(), _QUESTION_NAMES)(root)
    getattr(Contract(), _QUESTION_NAMES)(root)
    cut = populate_fixture_tree(_StoredNamesAreCutAndDecoded("/"))
    with pytest.raises((Exception, pytest.fail.Exception)) as failure:
        getattr(Contract(), _QUESTION_NAMES)(cut)
    assert not isinstance(failure.value, pytest.skip.Exception)


# --- a backend without an operation needs no expected-failure marks ------------

_NEEDS_MKDIR = {
    "test_mkdir_and_is_dir",
    "test_mkdir_existing_raises_file_exists",
    "test_mkdir_missing_parent_raises_file_not_found",
    "test_mkdir_parents",
    "test_rmdir_requires_empty",
    "test_rm_recursive",
    "test_rm_recursive_accepts_the_follow_policies",
    "test_rm_rejects_a_policy_that_is_not_one",
    "test_copy_recursive",
    "test_move_directory",
}
_NEEDS_DELETE = {
    "test_unlink",
    "test_unlink_missing_raises_then_missing_ok",
    "test_unlink_directory_raises",
    "test_rmdir_requires_empty",
    "test_rmdir_missing_raises_file_not_found",
    "test_rmdir_file_raises_not_a_directory",
    "test_rm_recursive",
    "test_rm_recursive_accepts_the_follow_policies",
    "test_rm_rejects_a_policy_that_is_not_one",
    "test_rm_non_recursive_directory_requires_empty",
    "test_rm_missing_ok",
}
_NEEDS_MOVE = {
    "test_move",
    "test_move_existing_target_raises_without_overwrite",
    "test_move_directory",
}


class _NoMkdirNoDeleteNoMove(MemPath):
    """Like a TFTP client: whole files can be read and written; a directory
    cannot be made, nothing deleted and nothing moved."""

    def _mkdir(self, mode):
        raise NotImplementedError("mkdir")

    def unlink(self, missing_ok=False):
        raise NotImplementedError("unlink")

    def rmdir(self):
        raise NotImplementedError("rmdir")

    def rm(self, *args, **kwargs):
        raise NotImplementedError("rm")

    def move(self, *args, **kwargs):
        raise NotImplementedError("move")


def _skipped_tests(contract, backend_cls=MemPath):
    skipped, failed = set(), {}
    for name in _contract_test_names(PathContract):
        # The tree is seeded through a backend that can make directories.
        seeded = populate_fixture_tree(MemPath("/"))
        root = backend_cls("/", backend=seeded.backend)
        try:
            getattr(contract(), name)(root)
        except pytest.skip.Exception:
            skipped.add(name)
        except Exception as error:
            failed[name] = error
    return skipped, failed


@pytest.mark.parametrize(
    "switch, tests",
    [
        ("supports_mkdir", _NEEDS_MKDIR),
        ("supports_delete", _NEEDS_DELETE),
        ("supports_move", _NEEDS_MOVE),
    ],
)
def test_each_operation_switch_skips_the_tests_that_need_it(switch, tests):
    contract = type(
        "Contract", (PathContract,), {"supports_rename": False, switch: False}
    )
    skipped, failed = _skipped_tests(contract)
    assert tests <= skipped
    assert failed == {}


def test_a_backend_with_no_mkdir_delete_or_move_needs_no_expected_failures():
    # Every test that would call one of the three is skipped, and the rest
    # pass, on a backend whose three operations raise.
    contract = type(
        "Contract",
        (PathContract,),
        {
            "supports_rename": False,
            "supports_mkdir": False,
            "supports_delete": False,
            "supports_move": False,
        },
    )
    skipped, failed = _skipped_tests(contract, _NoMkdirNoDeleteNoMove)
    assert failed == {}
    assert skipped >= _NEEDS_MKDIR | _NEEDS_DELETE | _NEEDS_MOVE
