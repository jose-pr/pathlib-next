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

Optional I/O, implement whichever your resource actually supports -- leave
the rest as the inherited `@notimplemented` stubs (derived helpers either
fall back, e.g. `move()` falls back to copy+unlink when `rename()` isn't
implemented, or raise `NotImplementedError` cleanly):

```python
iterdir()                       # yield child instances
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

`pathlib_next.testing.PathContract` is a pytest mixin covering the
baseline contract every `Path` implementation (either track) must satisfy
-- `mkdir`/`is_dir`, read/write round-trips, `iterdir`, `unlink`/`rmdir`
error semantics, `rm(recursive=)`, `copy`/`move`, `touch(exist_ok=False)`,
`mkdir(parents=)`. Subclass it with a `root` fixture:

```python
import pytest
from pathlib_next.testing import PathContract

class TestMyPath(PathContract):
    @pytest.fixture
    def root(self, tmp_path):
        return MyPath(tmp_path)   # an empty, writable directory
```

See `pathlib_next`'s own `tests/test_contract.py` for the worked example
running this against `LocalPath`, `MemPath`, and `FileUri` together.
