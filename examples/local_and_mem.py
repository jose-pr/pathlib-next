"""Local filesystem, MemPath, glob, Query/Source, and PathSyncer -- all
self-contained (no network, no external services). Run directly:

    python examples/local_and_mem.py
"""
import tempfile

from pathlib_next import Path, glob
from pathlib_next.mempath import MemPath
from pathlib_next.uri import Query, Source
from pathlib_next.utils.sync import PathAndStat, PathSyncer


def local_path_basics(root: Path):
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hi')")
    (root / "src" / "utils.py").write_text("def helper(): ...")
    (root / "README.md").write_text("# Example project")

    print("Python files:", [p.name for p in root.glob("**/*.py")])
    print("Top-level entries:", [p.name for p in root.iterdir()])


def mempath_basics():
    mempath = MemPath("test/test3") / "subpath"
    mempath.parent.mkdir(parents=True, exist_ok=True)
    mempath.write_text("test")
    print("MemPath round-trip:", mempath.read_text())
    mempath.parent.rm(recursive=True)


def query_and_source_basics():
    query = Query({"page": "1", "tags": ["a", "b"]})
    print("Query string:", str(query))
    print("Query decoded:", query.to_dict())

    source = Source(scheme="https", userinfo="user", host="example.com", port=443)
    print("Source:", str(source))


def glob_and_sync(root: Path):
    def checksum(entry: PathAndStat):
        return entry.stat.st_size

    mem_root = MemPath("/")
    (mem_root / "a.txt").write_text("aaa")
    (mem_root / "b.txt").write_text("bb")

    syncer = PathSyncer(checksum, remove_missing=True)
    syncer.sync(mem_root, root / "synced", dry_run=False)
    print("Synced files:", sorted(p.name for p in (root / "synced").iterdir()))

    for path in glob.glob(root / "**/*.py", recursive=True):
        print("Found via glob.glob():", path)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        local_path_basics(root)
        mempath_basics()
        query_and_source_basics()
        glob_and_sync(root)
