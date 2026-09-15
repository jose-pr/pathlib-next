# Extending

Two equally first-class ways to add a new path-addressable resource. In
both, you implement a small, documented method surface; everything else
(`open`/`read_text`/`write_text`/`glob`/`walk`/`touch`/`rm`/`copy`/`move`/
`exists`/`is_dir`/`is_file`/...) is *derived* automatically from the
protocols in `pathlib_next.protocols`.

- **Track A -- subclass `Path` directly**: for any custom path-addressable
  resource that isn't naturally a URI (e.g. a database-backed virtual
  filesystem, an archive member, a key-value store). `MemPath` is the
  reference exemplar.
- **Track B -- subclass `UriPath`**: for a new URI scheme (`http:`,
  `sftp:`, ...). Registers automatically and gets pure-path parsing
  (join, query, fragment) for free from `Uri`.

Whichever track you pick, run the shared contract test suite against your
implementation -- see [Testing your implementation](#testing-your-implementation)
below.

## Track A: subclass `Path`

Required (pure-path side, from the `Pathname` ABC):

```python
segments        # property -> sequence of path component strings
parts           # property -> whatever "parts" means for your type
parent          # property -> the logical parent
with_segments(*segments)   # construct a same-type instance from new segments
as_uri()        # a URI string identifying this path (can be a custom scheme)
relative_to(other)          # or raise NotImplementedError if not meaningful
```

Equality is **not** on that list: `Pathname` supplies a default `__eq__`/
`__hash__` keyed on `(type(self), tuple(self.segments))`, so your class is
usable as a dict key or set member, and the equality-based helpers
(`is_relative_to()`, `parents` membership) work, without you writing
anything. Override both together if your type needs a different identity
-- e.g. case-insensitive segments, or one that also distinguishes the
backing store two otherwise-identical paths point at. (`LocalPath` and the
`*Pathname` classes don't use this default: `pathlib.PurePath` precedes
`Pathname` in their MRO and keeps its own equality.)

Optional I/O, implement whichever your resource actually supports -- leave
the rest as the inherited `@notimplemented` stubs (derived helpers either
fall back, e.g. `move()` falls back to copy+unlink when `rename()` isn't
implemented, or raise `NotImplementedError` cleanly):

```python
iterdir()                       # yield child instances
_scandir()                      # optional: yield (name, FileStat|None) pairs
                                 #    instead, if listing your resource can
                                 #    cheaply include stat metadata -- speeds
                                 #    up walk()/glob() (see Track B's
                                 #    "_scandir: listing with metadata" below,
                                 #    which applies here too)
stat(*, follow_symlinks=True)   # -> a FileStatLike (utils.stat.FileStat is a
                                 #    ready-made concrete one)
_open(mode, buffering)          # -> a *binary* IOBase; open()/read_text()/
                                 #    write_bytes()/copy() are all derived
                                 #    from this one method
_mkdir(mode)                    # create just this directory (mkdir() layers
                                 #    parents=/exist_ok= handling on top)
unlink(), rmdir()
rename(target)
chmod(mode, *, follow_symlinks=True)
```

`MemPath` (`src/pathlib_next/mempath.py`) implements exactly this surface
over a backend of nested dicts (`MemPathBackend`; a `dict` value is a
directory, a `bytearray` value is a file) -- read it end to end as a
worked example; it's under 200 lines.

## Track B: subclass `UriPath`

The pure-path side (parsing, join, query/fragment, `with_*`) comes free
from `Uri`. Register your scheme and implement the I/O surface:

```python
from pathlib_next.uri import UriPath

class MyPath(UriPath):
    __SCHEMES = ("myscheme",)   # name-mangled per-class; redeclare in every
                                 # subclass, don't inherit it

    def _listdir(self):
        ...                      # yield child *names* (str), not instances --
                                  # UriPath.iterdir() wraps each into a child

    def stat(self, *, follow_symlinks=True):
        ...

    def _open(self, mode="r", buffering=-1):
        ...

    def _mkdir(self, mode): ...
    def unlink(self, missing_ok=False): ...
    def rmdir(self): ...
    def rename(self, target): ...
    def chmod(self, mode, *, follow_symlinks=True): ...
```

Importing the module that defines your subclass is enough to register it
(`UriPath._schemesmap()` walks `__subclasses__()` and caches the result) --
`UriPath("myscheme://host/path")` then dispatches to `MyPath` automatically.

### `_scandir`: listing with metadata

If your remote listing call already returns type/size/mtime for every
child in one round trip (an HTML directory index, WebDAV PROPFIND, SFTP
`listdir_attr`, FTP MLSD, an S3 `list_objects_v2` page, ...), override
`_scandir()` instead of (or alongside) `_listdir()`:

```python
def _scandir(self):
    for name, meta in my_one_shot_listing_call(self.path):
        yield name, FileStat(st_size=meta.size, st_mtime=meta.mtime,
                              is_dir=meta.is_dir)
```

`UriPath.iterdir()` is derived from `_scandir()` and pre-seeds each child
with its `FileStat` as a *single-use* hint: the child's first `stat()` call
returns the hint directly (no request), and every call after that re-fetches
for real -- so a live mutation is never masked by a stale value. `walk()`/
`glob()` then classify directories vs. files from this same hint, turning a
remote-tree walk from O(entries) round trips into O(dirs). If you don't
override `_scandir()`, it falls back to `_listdir()` + one `stat()` per
child (no round-trip savings, but nothing breaks) -- `_listdir()`/
`iterdir()` remain fully supported on their own for schemes that have no
richer listing call to offer. See `HttpPath`/`DavPath`/`SftpPath`/`FtpPath`/
`S3Path` (`src/pathlib_next/uri/schemes/`) for worked examples.

Optional: override `_initbackend()` to lazily create per-instance
connection/session state (see `HttpBackend`/`SftpBackend`/`MemPathBackend`
for the pattern -- a NamedTuple or small class holding a session/client,
propagated to children via `with_segments`/`_make_child_relpath`).

`FileUri`, `HttpPath`, and `SftpPath` (`src/pathlib_next/uri/schemes/`) are
the three built-in worked examples, in increasing order of complexity
(`FileUri` is ~70 lines wrapping `LocalPath`; `SftpPath` adds connection
pooling; `HttpPath` adds HTML-scraping-based listing and HEAD/GET stat
fallback).

## Testing your implementation

To ensure custom implementations comply with `pathlib_next`'s expected behaviors, the library offers three contract levels in `pathlib_next.testing` which can be mixed into your `pytest` suite:

1. **`PurePathContract`**: Covers logical pure-path operations (joining, parents, stems, suffix checks, and glob matching) that do not require any physical I/O.
2. **`ReadPathContract`**: Extends `PurePathContract` to verify read-only I/O: `exists()`/`is_file()`/`is_dir()`, `read_text()`/`read_bytes()`, `open()` read modes, `iterdir()`, `stat()`, `glob()`/`rglob()` and `walk()`, plus the error paths with pathlib's exception types (a missing path raises `FileNotFoundError`, listing a file `NotADirectoryError`, reading a directory `IsADirectoryError` or, as pathlib does on Windows, `PermissionError`).
3. **`PathContract`**: Extends `ReadPathContract` to verify writes: `mkdir()`, `write_text()`/`write_bytes()`, `open()` write/append/exclusive modes, `unlink()`, `rmdir()`, `rm()`, `copy()` (including `recursive=True`), `move()`, `rename()` and `touch()`, again with their error paths (a missing parent raises `FileNotFoundError`, `rmdir()` of a file `NotADirectoryError`, of a non-empty directory `OSError(ENOTEMPTY)`).

Both I/O levels need a `root` fixture pointing at a **fresh, function-scoped** directory holding the standard tree (`a.txt`, `b.py`, `.hidden.txt`, `sub/c.py`, `sub/nested/d.py`, `empty_dir/`). `populate_fixture_tree(root)` builds it through your path's own `mkdir()`/`write_text()`. The write tests create fixed names under `root` without cleaning up, so two contract classes must never share one.

### Example: Running the full contract

```python
import pytest

from pathlib_next import LocalPath as MyPath  # your Path subclass here
from pathlib_next.testing import PathContract, populate_fixture_tree


class TestMyPath(PathContract):
    @pytest.fixture
    def root(self, tmp_path):
        root = MyPath(tmp_path)
        populate_fixture_tree(root)
        return root
```

This example runs verbatim in the project's own suite (`tests/test_contract_helpers.py`).

### Capability attributes

A backend that genuinely cannot meet a rule sets the matching class attribute to `False` on its test class. The affected tests then report as skipped, never as passed. Every attribute defaults to `True`; set one only for a documented gap.

| Attribute | Contract | Covers | Built-in schemes that set it `False` |
| --- | --- | --- | --- |
| `supports_listing` | `ReadPathContract` | `iterdir()`, `glob()`, `walk()` | `DataUri` (a single resource) |
| `supports_empty_directories` | `ReadPathContract` | `empty_dir/` lists as empty | `GitHubPath`, `GitLabPath` (git trees cannot hold an empty directory) |
| `distinguishes_file_types` | `ReadPathContract` | listing a file raises `NotADirectoryError`; reading a directory raises | `HttpPath` (one URL serves an index page or a file) |
| `supports_rename` | `PathContract` | `rename()` | `MemPath` (`move()` copies instead) |
| `supports_append` | `PathContract` | `open("a")` | `ZipUri`, `DavPath`, `S3Path`, `GsPath`, `AzPath` |
| `supports_exclusive_create` | `PathContract` | `open("x")` | none |
| `enforces_directory_hierarchy` | `PathContract` | `mkdir()`/writes below a missing parent raise `FileNotFoundError`; writing a file over a directory raises | `S3Path`, `GsPath`, `AzPath` (directories are key prefixes) |

### Which contract runs against the built-in schemes?

`tests/test_contract.py` runs:

- **Full `PathContract`**: `LocalPath`, `MemPath`, `FileUri`, `ZipUri` (local outer archive), `FtpPath` (in-process pyftpdlib), `DavPath` (in-process WsgiDAV), `S3Path` (moto), `SftpPath` (in-process asyncssh server, with both the paramiko and asyncssh client backends), and `GsPath`/`AzPath` (in-process fake REST servers; they skip when the SDK is not installed).
- **`ReadPathContract`**: `HttpPath` (local HTTP server), `TarUri`, `DataUri` (listing skipped), and `GitHubPath`/`GitLabPath` (in-process fake APIs).

See `tests/test_contract.py` for how each scheme's `root` fixture is wired.
