"""Test-suite building blocks for verifying custom Path/UriPath
implementations satisfy the library's filesystem contract.

Not imported by `pathlib_next/__init__.py` -- this module requires pytest,
which is a test-only dependency. Import it explicitly. Example::

    import pytest

    from pathlib_next import LocalPath as MyPath  # your Path subclass here
    from pathlib_next.testing import PathContract, populate_fixture_tree


    class TestMyPath(PathContract):
        @pytest.fixture
        def root(self, tmp_path):
            root = MyPath(tmp_path)
            populate_fixture_tree(root)
            return root

`root` must be a **fresh, function-scoped** directory holding exactly the
standard tree (`populate_fixture_tree()` builds it through the path's own
API): the write tests create fixed names and assert their absence first, so
two contract classes sharing one root fail each other.

Rules pathlib guarantees are asserted with pathlib's exception types. Where
pathlib itself differs by OS (reading or unlinking a directory raises
`IsADirectoryError` on Linux and `PermissionError` on Windows/macOS), the
contract accepts that documented set. A backend that genuinely cannot meet a
rule sets the matching capability attribute to False on its test class; the
affected tests then report as skipped, never as passed:

- `ReadPathContract.supports_listing` -- `iterdir()`, `glob()`, `walk()`.
- `ReadPathContract.supports_empty_directories` -- `empty_dir/` lists as
  empty (git trees cannot hold an empty directory, so a placeholder file
  lives there).
- `ReadPathContract.distinguishes_file_types` -- listing a file raises
  `NotADirectoryError` and reading a directory raises (plain HTTP cannot
  tell: one URL serves an index page or a file).
- `PathContract.supports_rename` -- `rename()`.
- `PathContract.supports_append` -- `open("a")`/`open("ab")`.
- `PathContract.supports_exclusive_create` -- `open("x")`.
- `PathContract.enforces_directory_hierarchy` -- `mkdir()` and writes below
  a missing parent raise `FileNotFoundError`, and writing a file over a
  directory raises (object stores, whose directories are key prefixes,
  cannot).
"""

import errno as _errno

import pytest

#: The standard tree `ReadPathContract`/`PathContract` expect under `root`:
#: relative POSIX name -> file content, or `None` for a directory.
FIXTURE_TREE = {
    "a.txt": "a",
    "b.py": "b",
    ".hidden.txt": "hidden",
    "sub": None,
    "sub/c.py": "c",
    "sub/nested": None,
    "sub/nested/d.py": "d",
    "empty_dir": None,
}

#: What pathlib raises for a file operation (read, write, unlink) aimed at a
#: directory: `IsADirectoryError` on Linux, `PermissionError` on Windows and
#: (for unlink) macOS.
DIRECTORY_ERRORS = (IsADirectoryError, PermissionError)

#: errno values `rmdir()` of a non-empty directory may carry (POSIX allows
#: either; Windows maps ERROR_DIR_NOT_EMPTY to ENOTEMPTY).
NOT_EMPTY_ERRNOS = (_errno.ENOTEMPTY, _errno.EEXIST)


def populate_fixture_tree(root):
    """Create the standard contract tree (`FIXTURE_TREE`) under `root`, an
    existing empty directory, through the path's own `mkdir()` and
    `write_text()` -- works for any `Path` implementation (or a stdlib
    `pathlib.Path`). Returns `root`."""
    for name, content in FIXTURE_TREE.items():
        path = root.joinpath(*name.split("/"))
        if content is None:
            path.mkdir()
        else:
            path.write_text(content)
    return root


def _rel(root, path):
    """`path` relative to `root` as a POSIX string ("." for `root` itself).

    Built from `name`/`parent` only, so it holds for implementations whose
    `relative_to()` is missing or spells the root differently. A root URI
    with and without its trailing "/" (`http://host` vs `http://host/`)
    counts as the same directory."""
    names = []
    while path != root and str(path).rstrip("/") != str(root).rstrip("/"):
        parent = path.parent
        if parent == path or len(names) > 64:
            raise AssertionError(f"{path!r} is not below {root!r}")
        names.append(path.name)
        path = parent
    return "/".join(reversed(names)) or "."


class PurePathContract:
    """Contract tests for pure path (logical) operations, not requiring any I/O."""

    def test_pure_basics(self, root):
        p = root / "dir" / "file.txt"
        assert p.name == "file.txt"
        assert p.suffix == ".txt"
        assert p.stem == "file"

        # parent and parents
        assert p.parent.name == "dir"
        assert len(p.parents) >= 2
        assert p.parents[0].name == "dir"

    def test_pure_joinpath_truediv(self, root):
        p = root / "a"
        assert (p / "b").name == "b"
        assert p.joinpath("b", "c").name == "c"

    def test_pure_match(self, root):
        p = root / "dir" / "file.txt"
        assert p.match("*.txt")
        assert not p.match("*.py")


class ReadPathContract(PurePathContract):
    """Contract tests for read-only path operations.

    Subclasses must provide a `root` fixture pointing to a fresh directory
    pre-populated with the standard tree -- see `populate_fixture_tree()` and
    `FIXTURE_TREE`:

    - a.txt (content: "a")
    - b.py (content: "b")
    - .hidden.txt (content: "hidden")
    - sub/c.py (content: "c")
    - sub/nested/d.py (content: "d")
    - empty_dir/

    Capability attributes (all default True; set one False only for a
    documented gap, and the affected tests skip): `supports_listing`,
    `supports_empty_directories`, `distinguishes_file_types`.
    """

    supports_listing = True
    supports_empty_directories = True
    distinguishes_file_types = True

    def _require(self, capability):
        if not getattr(self, capability):
            pytest.skip(f"{type(self).__name__}.{capability} is False")

    def test_exists_and_types(self, root):
        assert root.exists()
        assert root.is_dir()

        a = root / "a.txt"
        assert a.exists()
        assert a.is_file()
        assert not a.is_dir()

        sub = root / "sub"
        assert sub.exists()
        assert sub.is_dir()
        assert not sub.is_file()

        assert not (root / "nonexistent").exists()
        assert not (root / "nonexistent").is_file()
        assert not (root / "nonexistent").is_dir()

    def test_read_text_and_bytes(self, root):
        assert (root / "a.txt").read_text() == "a"
        assert (root / "a.txt").read_bytes() == b"a"
        assert (root / "sub" / "c.py").read_text() == "c"
        assert (root / "sub" / "nested" / "d.py").read_bytes() == b"d"

    def test_open_read_modes(self, root):
        f = root / ".hidden.txt"
        with f.open() as fh:
            assert fh.read() == "hidden"
        with f.open("r") as fh:
            assert fh.read() == "hidden"
        with f.open("rt") as fh:
            assert fh.read() == "hidden"
        with f.open("rb") as fh:
            assert fh.read() == b"hidden"

    def test_iterdir_lists_children(self, root):
        self._require("supports_listing")
        names = {p.name for p in root.iterdir()}
        assert names == {"a.txt", "b.py", ".hidden.txt", "sub", "empty_dir"}
        if self.supports_empty_directories:
            assert list((root / "empty_dir").iterdir()) == []

    def test_iterdir_missing_raises_file_not_found(self, root):
        self._require("supports_listing")
        with pytest.raises(FileNotFoundError):
            list((root / "nonexistent").iterdir())

    def test_iterdir_file_raises_not_a_directory(self, root):
        self._require("supports_listing")
        self._require("distinguishes_file_types")
        with pytest.raises(NotADirectoryError):
            list((root / "a.txt").iterdir())

    def test_stat(self, root):
        st = (root / "a.txt").stat()
        assert st.st_size == 1

    def test_stat_missing_raises_file_not_found(self, root):
        with pytest.raises(FileNotFoundError):
            (root / "nonexistent").stat()

    def test_read_missing_raises_file_not_found(self, root):
        with pytest.raises(FileNotFoundError):
            (root / "nonexistent.txt").read_bytes()
        with pytest.raises(FileNotFoundError):
            (root / "nonexistent" / "x.txt").read_text()

    def test_read_directory_raises(self, root):
        self._require("distinguishes_file_types")
        with pytest.raises(DIRECTORY_ERRORS):
            (root / "sub").read_bytes()

    def test_glob(self, root):
        self._require("supports_listing")
        assert {_rel(root, p) for p in root.glob("*.py")} == {"b.py"}
        # pathlib matches hidden names with "*".
        assert {_rel(root, p) for p in root.glob("*")} == {
            "a.txt",
            "b.py",
            ".hidden.txt",
            "sub",
            "empty_dir",
        }
        assert {_rel(root, p) for p in root.glob("sub/*.py")} == {"sub/c.py"}
        # A trailing separator selects directories only.
        assert {_rel(root, p) for p in root.glob("*/")} == {"sub", "empty_dir"}

    def test_glob_recursive(self, root):
        self._require("supports_listing")
        expected = {"b.py", "sub/c.py", "sub/nested/d.py"}
        assert {_rel(root, p) for p in root.glob("**/*.py")} == expected
        assert {_rel(root, p) for p in root.rglob("*.py")} == expected

    def test_glob_missing_or_file_parent_selects_nothing(self, root):
        self._require("supports_listing")
        assert list((root / "nonexistent").glob("*")) == []
        assert list((root / "a.txt").glob("*")) == []
        assert list(root.glob("*.nonexistent")) == []

    def test_walk(self, root):
        self._require("supports_listing")
        expected = {
            ".": (["empty_dir", "sub"], [".hidden.txt", "a.txt", "b.py"]),
            "sub": (["nested"], ["c.py"]),
            "sub/nested": ([], ["d.py"]),
            "empty_dir": ([], []),
        }
        top_down = list(root.walk())
        actual = {
            _rel(root, d): (sorted(dirs), sorted(files)) for d, dirs, files in top_down
        }
        if not self.supports_empty_directories:
            del actual["empty_dir"], expected["empty_dir"]
        assert actual == expected
        assert _rel(root, top_down[0][0]) == "."

        bottom_up = [_rel(root, d) for d, _, _ in root.walk(top_down=False)]
        assert sorted(bottom_up) == sorted(_rel(root, d) for d, _, _ in top_down)
        assert bottom_up[-1] == "."
        assert bottom_up.index("sub/nested") < bottom_up.index("sub")


class PathContract(ReadPathContract):
    """Mixin of filesystem-contract tests every writable Path implementation
    (custom `Path` subclass, or `UriPath` scheme) must satisfy.

    Subclasses must provide a `root` fixture pointing to a fresh, writable,
    function-scoped directory pre-populated with the standard tree (see
    `populate_fixture_tree()`). Tests create fixed names under it without
    cleaning up.

    Capability attributes (all default True; set one False only for a
    documented gap): `supports_rename`, `supports_append`,
    `supports_exclusive_create`, `enforces_directory_hierarchy`.
    """

    supports_rename = True
    supports_append = True
    supports_exclusive_create = True
    enforces_directory_hierarchy = True

    def test_mkdir_and_is_dir(self, root):
        d = root / "new_dir"
        assert not d.exists()
        d.mkdir()
        assert d.is_dir()
        assert not d.is_file()

    def test_mkdir_existing_raises_file_exists(self, root):
        with pytest.raises(FileExistsError):
            (root / "sub").mkdir()
        (root / "sub").mkdir(exist_ok=True)
        # exist_ok never covers a file in the way.
        with pytest.raises(FileExistsError):
            (root / "a.txt").mkdir(exist_ok=True)
        assert (root / "a.txt").read_text() == "a"

    def test_mkdir_missing_parent_raises_file_not_found(self, root):
        self._require("enforces_directory_hierarchy")
        with pytest.raises(FileNotFoundError):
            (root / "missing_parent" / "child").mkdir()
        assert not (root / "missing_parent").exists()

    def test_write_read_text_roundtrip(self, root):
        f = root / "write_f.txt"
        f.write_text("hello")
        assert f.exists()
        assert f.is_file()
        assert not f.is_dir()
        assert f.read_text() == "hello"

    def test_write_read_bytes_roundtrip(self, root):
        f = root / "write_f.bin"
        f.write_bytes(b"\x00\x01hello")
        assert f.read_bytes() == b"\x00\x01hello"

    def test_write_missing_parent_raises_file_not_found(self, root):
        self._require("enforces_directory_hierarchy")
        with pytest.raises(FileNotFoundError):
            (root / "missing_parent" / "y.txt").write_text("y")
        assert not (root / "missing_parent").exists()

    def test_write_directory_raises(self, root):
        self._require("enforces_directory_hierarchy")
        with pytest.raises(DIRECTORY_ERRORS):
            (root / "sub").write_bytes(b"x")
        assert (root / "sub").is_dir()
        assert (root / "sub" / "c.py").read_text() == "c"

    def test_open_write_truncates(self, root):
        f = root / "write_trunc.txt"
        f.write_text("long content")
        with f.open("w") as fh:
            fh.write("x")
        assert f.read_text() == "x"
        with f.open("wt") as fh:
            fh.write("yz")
        assert f.read_text() == "yz"
        with f.open("wb") as fh:
            fh.write(b"\x00")
        assert f.read_bytes() == b"\x00"

    def test_open_append(self, root):
        self._require("supports_append")
        f = root / "append.txt"
        with f.open("a") as fh:  # creates a missing file
            fh.write("a")
        assert f.read_text() == "a"
        with f.open("a") as fh:
            fh.write("b")
        with f.open("ab") as fh:
            fh.write(b"c")
        assert f.read_text() == "abc"

    def test_open_exclusive_create(self, root):
        self._require("supports_exclusive_create")
        f = root / "exclusive.txt"
        with f.open("x") as fh:
            fh.write("new")
        assert f.read_text() == "new"
        with pytest.raises(FileExistsError):
            f.open("x")
        with pytest.raises(FileExistsError):
            f.open("xb")
        assert f.read_text() == "new"

    def test_unlink(self, root):
        f = root / "write_unlink.txt"
        f.write_text("x")
        assert f.exists()
        f.unlink()
        assert not f.exists()

    def test_unlink_missing_raises_then_missing_ok(self, root):
        f = root / "missing_unlink.txt"
        with pytest.raises(FileNotFoundError):
            f.unlink()
        f.unlink(missing_ok=True)

    def test_unlink_directory_raises(self, root):
        with pytest.raises(DIRECTORY_ERRORS):
            (root / "empty_dir").unlink()
        assert (root / "empty_dir").is_dir()

    def test_rmdir_requires_empty(self, root):
        d = root / "new_rmdir"
        d.mkdir()
        (d / "f.txt").write_text("x")
        with pytest.raises(OSError) as info:
            d.rmdir()
        assert info.value.errno in NOT_EMPTY_ERRNOS
        assert (d / "f.txt").read_text() == "x"
        (d / "f.txt").unlink()
        d.rmdir()
        assert not d.exists()

    def test_rmdir_missing_raises_file_not_found(self, root):
        with pytest.raises(FileNotFoundError):
            (root / "missing_rmdir").rmdir()

    def test_rmdir_file_raises_not_a_directory(self, root):
        with pytest.raises(NotADirectoryError):
            (root / "a.txt").rmdir()
        assert (root / "a.txt").read_text() == "a"

    def test_rm_recursive(self, root):
        d = root / "new_rm_rec"
        d.mkdir()
        (d / "f.txt").write_text("x")
        (d / "sub_rec").mkdir()
        (d / "sub_rec" / "g.txt").write_text("y")
        d.rm(recursive=True)
        assert not d.exists()

    def test_rm_non_recursive_directory_requires_empty(self, root):
        with pytest.raises(OSError) as info:
            (root / "sub").rm()
        assert info.value.errno in NOT_EMPTY_ERRNOS
        assert (root / "sub" / "c.py").exists()
        (root / "empty_dir").rm()
        assert not (root / "empty_dir").exists()

    def test_rm_missing_ok(self, root):
        with pytest.raises(FileNotFoundError):
            (root / "missing_rm").rm()
        (root / "missing_rm").rm(missing_ok=True)

    def test_copy_preserves_source(self, root):
        src = root / "a.txt"
        dst = root / "dst_copy.txt"
        src.copy(dst)
        assert dst.read_text() == "a"
        assert src.exists()

    def test_copy_existing_target_raises_without_overwrite(self, root):
        src = root / "a.txt"
        dst = root / "dst_copy_existing.txt"
        dst.write_text("existing")
        with pytest.raises(FileExistsError):
            src.copy(dst)
        assert dst.read_text() == "existing"
        src.copy(dst, overwrite=True)
        assert dst.read_text() == "a"

    def test_copy_missing_source_raises_file_not_found(self, root):
        dst = root / "dst_copy_missing.txt"
        with pytest.raises(FileNotFoundError):
            (root / "nonexistent.txt").copy(dst)
        assert not dst.exists()

    def test_copy_recursive(self, root):
        dst = root / "sub_copy"
        (root / "sub").copy(dst, recursive=True)
        assert (dst / "c.py").read_text() == "c"
        assert (dst / "nested" / "d.py").read_text() == "d"
        assert (root / "sub" / "nested" / "d.py").exists()

        (root / "sub" / "c.py").write_text("changed")
        with pytest.raises(FileExistsError):
            (root / "sub").copy(dst, recursive=True)
        assert (dst / "c.py").read_text() == "c"
        (root / "sub").copy(dst, recursive=True, overwrite=True)
        assert (dst / "c.py").read_text() == "changed"
        assert (dst / "nested" / "d.py").read_text() == "d"

    def test_move(self, root):
        # We write a temp file to move so we don't destroy a.txt for other tests
        src = root / "src_move.txt"
        src.write_text("data")
        dst = root / "dst_move.txt"
        src.move(dst)
        assert not src.exists()
        assert dst.read_text() == "data"

    def test_move_existing_target_raises_without_overwrite(self, root):
        src = root / "src_move_existing.txt"
        src.write_text("data")
        dst = root / "b.py"
        with pytest.raises(FileExistsError):
            src.move(dst)
        assert src.read_text() == "data"
        assert dst.read_text() == "b"
        src.move(dst, overwrite=True)
        assert not src.exists()
        assert dst.read_text() == "data"

    def test_move_directory(self, root):
        src = root / "sub"
        dst = root / "moved_sub"
        src.move(dst)
        assert not src.exists()
        assert (dst / "c.py").read_text() == "c"
        assert (dst / "nested" / "d.py").read_text() == "d"

    def test_rename(self, root):
        self._require("supports_rename")
        src = root / "src_rename.txt"
        src.write_text("data")
        dst = root / "dst_rename.txt"
        result = src.rename(dst)
        assert result.name == "dst_rename.txt"
        assert not src.exists()
        assert dst.read_text() == "data"
        with pytest.raises(FileNotFoundError):
            src.rename(root / "never.txt")

    def test_touch(self, root):
        f = root / "touch_new.txt"
        f.touch()
        assert f.is_file()
        assert f.read_bytes() == b""
        (root / "a.txt").touch()
        assert (root / "a.txt").read_text() == "a"

    def test_touch_exist_ok_false_raises_without_truncating(self, root):
        f = root / "touch_f.txt"
        f.write_text("keep")
        with pytest.raises(FileExistsError):
            f.touch(exist_ok=False)
        assert f.read_text() == "keep"

    def test_mkdir_parents(self, root):
        d = root / "parent_a" / "parent_b" / "parent_c"
        d.mkdir(parents=True)
        assert d.is_dir()
        with pytest.raises(FileExistsError):
            d.mkdir(parents=True, exist_ok=False)
        d.mkdir(parents=True, exist_ok=True)

    def test_listing_reflects_writes(self, root):
        self._require("supports_listing")
        (root / "empty_dir" / "new.txt").write_text("n")
        (root / "empty_dir" / "new_sub").mkdir()
        assert {p.name for p in (root / "empty_dir").iterdir()} == {
            "new.txt",
            "new_sub",
        }
        (root / "empty_dir" / "new.txt").unlink()
        (root / "empty_dir" / "new_sub").rmdir()
        assert list((root / "empty_dir").iterdir()) == []
