"""A name from a listing stays one component inside the directory that listed
it, for every consumer that turns a listed name into a child:
`rm(recursive=True)`, `walk()` and `PathSyncer`.

The directory under test lists a real temporary directory plus the rows a
server could invent (`..`, `.`, `a/b`, an empty name), so the damage a join
would do is visible on disk. It runs as a `LocalPath` subclass (where the
operating system resolves a joined `..`) and as a `file:` URI subclass (where
`/` itself resolves it).
"""

import itertools

import pytest

from pathlib_next import LocalPath
from pathlib_next.mempath import MemPath
from pathlib_next.utils.stat import FileStat
from pathlib_next.utils.sync import PathSyncer, SyncEvent

UNSAFE = ["a/b", "..", ".", ""]


def _listing_class(base, *, unsafe_first):
    """`base` with the unsafe rows added to the listing of every directory
    named `tree`."""

    class Listed(base):
        __SCHEMES = ()
        __slots__ = ()

        def _scandir(self):
            extra = (
                [(name, FileStat(is_dir=True)) for name in UNSAFE]
                if self.name == "tree"
                else []
            )
            if unsafe_first:
                yield from extra
            yield from super()._scandir()
            if not unsafe_first:
                yield from extra

    return Listed


@pytest.fixture(params=["local", "file-uri"])
def make_tree(request, tmp_path):
    """`make_tree(unsafe_first=False) -> (victim, tree)`: `victim/tree` is the
    directory the operation is given, beside `victim/sibling` and
    `victim/precious.txt`, which it must never touch."""
    if request.param == "file-uri":
        pytest.importorskip("uritools")
        from pathlib_next.uri.schemes.file import FileUri

        base = FileUri
    else:
        base = LocalPath

    def make(unsafe_first=False):
        victim = tmp_path / "victim"
        (victim / "tree" / "sub").mkdir(parents=True)
        (victim / "sibling").mkdir()
        for name in (
            "tree/a.txt",
            "tree/sub/b.txt",
            "sibling/keep.txt",
            "precious.txt",
        ):
            (victim / name).write_text(name)
        cls = _listing_class(base, unsafe_first=unsafe_first)
        tree = victim / "tree"
        return victim, cls(tree if base is LocalPath else tree.as_uri())

    return make


def _files(root):
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


class _Runaway(BaseException):
    """Raised by a test hook to stop an operation that would not end."""


def test_rm_recursive_offers_each_unsafe_name_to_ignore_error(make_tree):
    victim, tree = make_tree()
    offered = []

    def ignore(error, path):
        offered.append((type(error), str(error)))
        return True

    tree.rm(recursive=True, ignore_error=ignore)

    assert [kind for kind, _ in offered] == [ValueError] * len(UNSAFE)
    for name, (_, message) in zip(UNSAFE, offered):
        assert repr(name) in message
    assert _files(victim) == ["precious.txt", "sibling/keep.txt"]
    assert not (victim / "tree").exists()


def test_rm_recursive_without_a_handler_stops_before_removing_anything(make_tree):
    victim, tree = make_tree(unsafe_first=True)

    with pytest.raises(ValueError, match="unsafe child name"):
        tree.rm(recursive=True)

    assert _files(victim) == [
        "precious.txt",
        "sibling/keep.txt",
        "tree/a.txt",
        "tree/sub/b.txt",
    ]


def test_walk_omits_unsafe_names_and_reports_them_to_on_error(make_tree):
    _, tree = make_tree()
    errors = []

    walked = itertools.islice(tree.walk(on_error=errors.append), 20)
    rows = [(path.name, sorted(dirs), sorted(files)) for path, dirs, files in walked]

    assert rows == [("tree", ["sub"], ["a.txt"]), ("sub", [], ["b.txt"])]
    assert [type(error) for error in errors] == [ValueError] * len(UNSAFE)
    for name, error in zip(UNSAFE, errors):
        assert repr(name) in str(error)
        assert error.filename == str(tree)


def test_walk_without_on_error_omits_unsafe_names_in_silence(make_tree):
    _, tree = make_tree()

    walked = itertools.islice(tree.walk(top_down=False), 20)

    assert [(path.name, sorted(dirs)) for path, dirs, _ in walked] == [
        ("sub", []),
        ("tree", ["sub"]),
    ]


def test_sync_remove_missing_never_removes_an_unsafe_target_name(make_tree):
    victim, target = make_tree()
    source = MemPath("/src")
    source.mkdir()
    (source / "a.txt").write_text("tree/a.txt")
    refused = []

    def ignore(error, source_entry, target_entry, event):
        if isinstance(error, ValueError):
            refused.append((str(error), event))
        return True

    PathSyncer(
        lambda entry: entry.stat.st_size, remove_missing=True, ignore_error=ignore
    ).sync(source, target)

    assert [event for _, event in refused] == [SyncEvent.RemovedMissing] * len(UNSAFE)
    for name, (message, _) in zip(UNSAFE, refused):
        assert repr(name) in message
    # `sub` is not in the source, so it goes; nothing outside `tree` does.
    assert _files(victim) == ["precious.txt", "sibling/keep.txt", "tree/a.txt"]


def test_sync_from_a_listing_copies_only_the_safe_names(make_tree, tmp_path):
    _, source = make_tree()
    target = LocalPath(tmp_path / "dst")
    refused = []
    copied = []

    def ignore(error, source_entry, target_entry, event):
        if isinstance(error, ValueError):
            refused.append((str(error), event))
        return True

    def hook(source_entry, target_entry, event, dry_run):
        if event is SyncEvent.Copy:
            copied.append(source_entry.path.name)
            if len(copied) > 10:
                raise _Runaway

    PathSyncer(lambda entry: entry.stat.st_size, hook=hook, ignore_error=ignore).sync(
        source, target
    )

    assert [event for _, event in refused] == [SyncEvent.SyncChild] * len(UNSAFE)
    for name, (message, _) in zip(UNSAFE, refused):
        assert repr(name) in message
    assert sorted(copied) == ["a.txt", "b.txt"]
    assert _files(target) == ["a.txt", "sub/b.txt"]


def test_sync_without_a_handler_raises_on_an_unsafe_name(make_tree):
    victim, target = make_tree(unsafe_first=True)
    source = MemPath("/src")
    source.mkdir()

    with pytest.raises(ValueError, match="unsafe child name"):
        PathSyncer(remove_missing=True).sync(source, target)

    assert _files(victim) == [
        "precious.txt",
        "sibling/keep.txt",
        "tree/a.txt",
        "tree/sub/b.txt",
    ]
