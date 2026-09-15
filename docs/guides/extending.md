# Extending

Two equally first-class ways to add a new path-addressable resource. In both,
you implement a small method surface; everything else
(`open`/`read_text`/`write_text`/`glob`/`walk`/`touch`/`rm`/`copy`/`move`/
`exists`/`is_dir`/`is_file`/...) is *derived* from the protocols in
`pathlib_next.protocols`.

- **Track A -- subclass `Path` directly**: for a resource that is not
  naturally a URI (a database-backed virtual filesystem, a key-value store,
  ...). `MemPath` is the reference implementation.
- **Track B -- subclass `UriPath`**: for a new URI scheme. The class registers
  itself and gets URI parsing, joining, query and fragment handling from
  `Uri`.

Whichever track you pick, run the shared contract suite against your
implementation -- see [Testing your implementation](#testing-your-implementation)
below.

## Track A: subclass `Path`

Required on the pure-path side (abstract on `Pathname`):

| Member | Contract |
| --- | --- |
| `segments` | property: the path components; a leading `""` marks an absolute path |
| `parts` | property: whatever "parts" means for your type |
| `parent` | property: the logical parent (the root is its own parent) |
| `with_segments(*segments)` | a same-type instance, keeping per-instance state such as a backend |
| `as_uri()` | a URI string identifying the path (a custom scheme is fine) |
| `relative_to(other)` | may raise `NotImplementedError` if not meaningful |

`is_absolute()` is an optional stub that raises `NotImplementedError` until you
override it. Equality is not required either: `Pathname` supplies
`__eq__`/`__hash__` keyed on `(type(self), tuple(self.segments))`, so paths
work as dict keys and `is_relative_to()`/`parents` membership work. Override
both together if your type needs another identity (case-insensitive names, or
one that distinguishes two backing stores).

On the I/O side, implement what the resource supports and leave the rest as
the inherited stubs. Derived operations either fall back (`move()` copies and
deletes when `rename()` is missing) or raise `NotImplementedError`:

| Method | Contract |
| --- | --- |
| `stat(*, follow_symlinks=True)` | a `FileStatLike`; `utils.stat.FileStat(st_mode=None, st_size=0, st_mtime=0, is_dir=False)` is ready-made. Raise `FileNotFoundError` for a missing path. |
| `iterdir()` | yield child instances; raise `FileNotFoundError`/`NotADirectoryError` like pathlib. Required for any listing: `copy(recursive=True)` and `for child in path` call it, and the default `_scandir()` is built on it. |
| `_scandir()` | optional addition: yield `(name, FileStat or None)` when the listing already carries metadata. `walk()`, `glob()`, `rm(recursive=True)` and `PathSyncer` use it instead of a `stat()` per child. The stat is non-following; `None` means "unknown". |
| `_open(mode, buffering)` | a **binary** stream. `open()` validates the mode and passes a canonical `"r"`, `"w"`, `"x"` or `"a"`, optionally with `"+"` (never `"b"`/`"t"`); raise `NotImplementedError` for a mode you do not support. `read_text()`, `write_bytes()`, `copy()`, `touch()` all derive from it. |
| `_mkdir(mode)` | create this one directory: `FileExistsError` if it exists, `FileNotFoundError` if the parent is missing (`mkdir(parents=True)` relies on it). |
| `unlink(missing_ok=False)`, `rmdir()` | pathlib's exceptions: `IsADirectoryError`, `NotADirectoryError`, `OSError(errno.ENOTEMPTY)`. |
| `rename(target)` | return the new path. |
| `chmod(mode, *, follow_symlinks=True)` | normalize with `utils.as_mode(mode)` so octal strings work. |
| `_symlink_to(target, target_is_directory=False)`, `readlink()` | `symlink_to(..., force=)` is derived; `target` is already a path object. |
| `_chown(uid, gid, *, follow_symlinks=True)` | receives a canonical pair (`None` = unchanged); `chown()` is derived. |

When a method accepts a `str` path, turn it into a path with
`self.with_segments(value)`, never `type(self)(value)`, which drops
per-instance state (for `MemPath`, the whole in-memory tree). `copy()` and
`move()` convert a `str` destination through `_coerce_target()`, which does
exactly that by default.

`MemPath` implements this surface over nested dicts (`MemPathBackend`: a
`dict` value is a directory, a `bytearray` a file); see
[Memory Path API](../api/mempath.md).

## Track B: subclass `UriPath`

The pure-path side (parsing, join, query/fragment, `with_*`) comes from `Uri`.
Register the scheme and implement the I/O surface:

```python
import errno
import os

from pathlib_next.uri import UriPath
from pathlib_next.utils.stat import FileStat


class MyPath(UriPath):
    __SCHEMES = ("myscheme",)  # name-mangled: declare it in every class

    def _initbackend(self):
        # Connection or session state, created on first use and shared by
        # every path derived from this one on the same endpoint.
        return None

    def _scandir(self):
        # Or implement `_listdir()` to yield names only.
        yield from ()

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()  # metadata from the parent's listing
        if hint is not None:
            return hint
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(self))

    def _open(self, mode="r", buffering=-1):
        raise NotImplementedError(f"open(mode={mode!r})")

    def rename(self, target):
        target = self._rename_target(target)  # sibling str, same endpoint only
        ...  # rename self.path to target.path on the server
        return self.with_path(target.path)


print(type(UriPath("myscheme://host/data/file.txt")).__name__)
```

Defining (importing) the subclass is enough: `UriPath("myscheme://...")`
dispatches to it, also when the class is defined after the first dispatch.
The class name must not start with an underscore, or the name-mangled
`__SCHEMES` lookup misses it. A distribution can also register the class
without an import, through an entry point:

```toml
[project.entry-points."pathlib_next.schemes"]
myscheme = "mypackage.paths:MyPath"
```

Conventions for a scheme implementation:

- Use `self.path` (percent-decoded) on the wire and `self.source`
  (`scheme`, `userinfo`, `host`, `port`, `parsed_userinfo()`) for the
  connection. `str(self)` is already redacted for error messages.
- `iterdir()` is derived from `_scandir()`, which defaults to `_listdir()`
  plus one `stat()` per child. Override `_scandir()` when one listing call
  returns type, size and mtime (an HTML index, `PROPFIND`, `listdir_attr`,
  `MLSD`, an S3 list page): each child's first `stat()` then returns the
  listing's metadata through `_pop_stat_hint()`, and later calls fetch
  again, so a remote walk costs one request per directory.
- `rename()` and `symlink_to()` receive a `str` as a decoded path, not URI
  syntax; `_rename_target()` resolves a relative one against the parent and
  raises `NotImplementedError` for another endpoint, which makes `move()`
  copy instead. Override `_same_location()` when your namespace is narrower
  than the authority (an archive, a container).
- A backend is inherited by derived paths only for the same scheme, userinfo,
  host and port; paths elsewhere call `_initbackend()` again.
- Set `_host_filesystem_path = True` only when `self.path` is a real
  filesystem path on the remote host (as for `sftp:`); `os.fspath()` and
  `host_fspath()` then return it.

The built-in schemes are worked examples of increasing size:
[`FileUri`](../api/schemes/local.md) delegates to `LocalPath`,
[`S3Path`](../api/schemes/objstore.md) emulates directories over key
prefixes, [`HttpPath`](../api/schemes/http.md) scrapes HTML listings behind a
`requests` session, and [`SftpPath`](../api/schemes/sftp.md) caches
connections behind two interchangeable backends.

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
