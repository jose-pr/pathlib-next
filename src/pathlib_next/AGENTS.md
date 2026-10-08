# `pathlib_next` — public API header

Header-file-style reference for the `pathlib_next` package: every public export
with its signature, arguments, contract and gotchas, so the package can be used
without reading its source. It ships inside the package and is self-contained.
Development documentation lives with the source at
<https://github.com/jose-pr/pathlib-next>; the rendered documentation is at
<https://jose-pr.github.io/pathlib-next/> and the project overview is the
`README.md` shipped next to this file (`pathlib_next/README.md`). Every
deliberate behavioral difference from `pathlib.Path` is listed, with its
rationale, at <https://jose-pr.github.io/pathlib-next/divergences/>; this file
states the resulting contract.

Names are imported from the module named in the heading of the section that
documents them; the root `pathlib_next` re-exports the core ones ("Package
root"). A module or name that starts with an underscore is private: where this
file names one (`pathlib_next.uri.schemes._gitrepo`,
`pathlib_next.uri.schemes.archive._base`) it names the public names to use from
it, and nothing else there is contract. The file has two parts: the core
(`Package root` through `Testing helpers`) and the URI layer (`pathlib_next.uri`
and every scheme, from `URIs`), each readable without the other; the closing
sections (`Exceptions`, `Command line`, `Environment variables`, `Gotchas`)
keep the core and the URI layer under separate headings.

`pip install pathlib-next[<extras>]`. No required dependencies.

| Extra | Installs | Needed for |
| --- | --- | --- |
| *(none)* | — | `Path`, `LocalPath`, `MemPath`, `utils`, `testing`, `uripath` on local paths |
| `uri` | `uritools`, `netimps>=0.4.0,<0.5` | `pathlib_next.uri` and **every** URI scheme (`file:`, `data:`, `ftp(s):`, archives included) |
| `http` | `requests` + `uri` | `http(s):`, `dav(s):`, `github:`, `gitlab:`, `git:` |
| `sftp` | `paramiko` + `uri` | `sftp:` (paramiko backend) |
| `sftp-async` | `asyncssh` + `uri` | `sftp:` (asyncssh backend) |
| `s3` / `gs` / `az` | `boto3` / `google-cloud-storage` / `azure-storage-blob` + `uri` | `s3:` / `gs:` / `az:` |

`pathlib_next.__version__` is the installed distribution's version
(`importlib.metadata`), `"0+unknown"` for a source tree that is not installed.

## Package root (`pathlib_next`)

`import pathlib_next` exposes `Path`, `Pathname`, `LocalPath`,
`PosixPathname`, `WindowsPathname`, `FileStat`, the protocols `Stat`, `Chmod`,
`BinaryOpen`, `FsPathLike`, the aliases `PathLike`/`PurePathLike`, and the modules
`glob` (`utils.glob`) and `sync` (`utils.sync`). `MemPath` lives in
`pathlib_next.mempath`; `pathlib_next.testing` is never imported implicitly.

The root, `path`, `fspath`, `protocols` (and its `fs`, `io`, `checksum`),
`utils`, `utils.stat` and `utils.glob` declare `__all__` with exactly the names
this file documents for them, so `from pathlib_next import *` publishes those
and nothing else (`P` and `PN` are not among them; they stay importable from
`pathlib_next.path`).

The package ships `py.typed`: `Path(...)`, `LocalPath(...)`, `MemPath(...)` and
`FileStat(...)` pass mypy and pyright at their defaults. A protocol stub (`stat`,
`chmod`, `_chown`, `_open`, `checksum`, `__fspath__`) is an ordinary method that
raises `NotImplementedError`, and a type checker reads `Pathname`'s abstract
members as ordinary methods too, because the bare `Path(...)` builds a
`LocalPath`: a subclass that leaves one out is refused when it is instantiated,
not by a checker.

### URI layer

`pathlib_next` also exposes `Uri`/`UriPath` **only when `uritools` is
importable**. Without the `uri` extra `hasattr(pathlib_next,
"UriPath")` is False, `pathlib_next.UriPath` raises an `AttributeError` that
names the extra, and `import pathlib_next.uri` raises a `ModuleNotFoundError`
that does (`from pathlib_next import UriPath` raises Python's own
`ImportError`).

A scheme class always imports, constructs, joins and compares, whether or not
its client library is installed. The first operation that needs the library
(`exists()`, `stat()`, a read, a listing, `is_local()` without `netimps`)
raises an `ImportError` that names the extra:
`pip install "pathlib-next[http]"`.

## Pure paths and I/O paths (`pathlib_next.path`)

### `Pathname`

```python
class Pathname(FsPathLike):
    # abstract
    segments: Sequence[str]
    parts
    parent: Self
    def with_segments(self, *segments) -> Self: ...
    def as_uri(self) -> str: ...
    def relative_to(self, other) -> Self: ...
    # derived
    name: str
    suffix: str
    suffixes
    stem
    def with_name(self, name) -> Self: ...
    def with_stem(self, stem) -> Self: ...
    def with_suffix(self, suffix) -> Self: ...
    parents: Sequence[Self]
    def is_relative_to(self, other): ...
    def joinpath(self, *args) -> Self: ...
    root: str
    drive: str
    anchor: str
    def match(self, path_pattern, *, case_sensitive=None): ...
    def full_match(self, pattern, *, case_sensitive=None) -> bool: ...
    def as_posix(self) -> str: ...
    def has_glob_pattern(self): ...
    def is_absolute(self) -> bool: ...
```

ABC for a pure (no I/O) path. Abstract: `segments`,
`parts`, `parent`, `with_segments()`, `as_uri()`,
`relative_to()`. Derived: `name`, `suffix`, `suffixes`, `stem`
(suffix rules of the running interpreter), `with_name`/`with_stem`/
`with_suffix` (`ValueError` for `""`, `.` or a separator; from 3.13
`with_stem("")` also raises when the name has a suffix), `parents`,
`is_relative_to()`, `joinpath()`, `/` and `"prefix" / path`,
`root`/`drive`/`anchor` (`root` is `"/"` when the first segment is empty;
`drive` is `""`), `match()` (pathlib's
right-anchored per-segment match; empty pattern → `ValueError`),
`full_match()` (3.13 semantics: the root is
a component of its own, so a rooted path needs a rooted pattern or a leading
`**` that reaches past it, and a lone `*` never matches it; `.` and empty
components of the pattern are ignored; a bracket expression never matches a
separator), `as_posix()`, `has_glob_pattern()`, `is_absolute()` (the path
has a root; a class whose rooted paths are not all absolute overrides it).

- `__eq__`/`__hash__` default to `(type(self), tuple(self.segments))`: exact
  type, so a subclass never equals its base. `LocalPath`/`PosixPathname`/
  `WindowsPathname` keep `pathlib.PurePath` equality; `Uri` compares its URI
  text. Override both together.
- `<`, `<=`, `>`, `>=` compare `_order_key()` (default: the segments; `Uri`:
  its URI text) between two paths of the same exact type, so `sorted()` works
  over `MemPath` and `Uri`; any other operand gives `NotImplemented`
  (`TypeError`), as `pathlib` does across flavours. A type with its own
  `__eq__` overrides `_order_key()` with the value that `__eq__` compares.
  `LocalPath`/`PosixPathname`/`WindowsPathname` keep `pathlib`'s ordering.
### `Path`

```python
class Path(Pathname, Chmod, Stat, BinaryOpen):
    def is_hidden(self): ...
    def is_junction(self) -> bool: ...
    def is_mount(self) -> bool: ...
    def is_dir_binding(self) -> bool: ...
    def samefile(self, other_path): ...
    def iterdir(self) -> Iterator[Self]: ...
    def glob(
        self, pattern, *, case_sensitive=None, include_hidden=True,
        recursive=None, dironly=None, recurse_symlinks=False, native=True,
        on_error=None, bound_loops=False,
    ): ...
    def rglob(
        self, pattern, *, case_sensitive=None, include_hidden=True,
        recursive=True, dironly=None, recurse_symlinks=False, native=True,
        on_error=None, bound_loops=False,
    ): ...
    def walk(self, top_down=True, on_error=None, follow_symlinks=False): ...
    def touch(self, mode=None, exist_ok=True): ...
    def mkdir(self, mode=0o777, parents=False, exist_ok=False): ...
    def unlink(self, missing_ok=False): ...
    def rmdir(self): ...
    def rm(
        self, recursive=False, missing_ok=False, ignore_error=False, *,
        follow_symlinks=False, follow_binds=False,
    ): ...
    def rename(self, target): ...
    def symlink_to(
        self, target, target_is_directory=False, *, force=False,
    ) -> None: ...
    def copy(
        self, target, *, overwrite=False, follow_symlinks=True,
        preserve_metadata=True, recursive=False, ignore_error=None,
        progress=None,
    ): ...
    def copy_into(
        self, target_dir, *, overwrite=False, follow_symlinks=True,
        preserve_metadata=True, recursive=False, ignore_error=None,
        progress=None,
    ) -> Path: ...
    def move_into(self, target_dir, *, overwrite=False) -> Path: ...
    def replace(self, target) -> Path: ...
    def move(self, target, *, overwrite=False): ...
    # override hooks
    def _same_filesystem(self, other) -> bool: ...
    def _node_key(self) -> tuple[object, tuple[str, ...]] | None: ...
    def _scandir(self) -> Iterator[Tuple[str, Optional[FileStat]]]: ...
    def _mkdir(self, mode): ...
    def _symlink_to(self, target, target_is_directory=False) -> None: ...
    def _coerce_target(self, target) -> Path: ...
    def _rename_compatible(self, target) -> bool: ...
    def _symlink_target(self, target) -> Self: ...
```

Base class for I/O paths.
`Path(*args)` on the bare class constructs a `LocalPath`.

- Operation precedence: `Path.__init_subclass__` re-asserts pathlib_next's
  `copy`, `move`, `copy_into`, `move_into`, `exists`, `rglob`, `read_text`,
  `write_text` and `symlink_to` on any subclass that would otherwise inherit stdlib
  `pathlib`'s. A class mixing a concrete stdlib path with `Path` but not
  `LocalPath` is a local class too: it gets every function `LocalPath`
  defines (`stat`, `chmod`, `is_junction`, `is_mount`, `glob`, `walk`,
  `_scandir`, `_symlink_to`, `_chown`, `is_dir`/`is_file` before 3.13, ...)
  wherever stdlib or a generic default would answer, so `rm(recursive=True)`
  stays out of a junction there as on `LocalPath`. A method defined in the
  subclass itself, or in a mixin of your own, always wins.
- `is_hidden()` — name starts with `"."`. `__iter__()` is `iterdir()`.
- `samefile()` — compares `(st_dev, st_ino)`; `NotImplementedError`
  when `stat()` lacks them (`LocalPath` uses pathlib's).
- `_same_filesystem()` — override hook: whether `other`'s
  segments are resolved in the same namespace (host, store, tree) as this
  path's. `==` ignores that, so `copy()`, `move()` and `PathSyncer` ask it
  before treating two paths as the same file, nested, or overlapping.
  Called only with `type(other) is type(self)`; must be symmetric and do no
  I/O. Default: both paths share one `_backend` object, or neither has
  one, so equal paths of a type that says nothing ARE the same file. **A
  type whose instances can front different hosts or stores must override
  it**, or a transfer between two of them spelled alike is refused
  (`OSError(EINVAL)` "same file" / `ValueError` "overlap"). `MemPath`:
  same `MemPathBackend` instance; `UriPath`: see `backend` below.
- `_node_key()` — override hook for a type
  whose segments spell one node several ways (`MemPath`: `a.txt`,
  `/a.txt`, `/d/../a.txt`; an archive member: `zip:` and `archive:`
  spellings, a query). `namespace` is an object two paths share exactly
  when they resolve names in one tree (compared with `is`); `names` is the
  node's normalized position in it (`()` for the root). `copy()` and
  `move()` compare two paths that both answer it, whatever their classes.
  Default `None`: two paths of one type that `_same_filesystem()` places
  together are compared with `==` and `is_relative_to()`. No I/O.
- `iterdir()` — **stub** (`NotImplementedError`); a
  listable `Path` must implement it (or, on `UriPath`, `_listdir()`/
  `_scandir()`).
- `_scandir()` — non-following stat
  per entry; default: `iterdir()` + one `stat(follow_symlinks=False)` per
  child. Consumed by `walk()`, `glob()`, `rm(recursive=True)` and
  `PathSyncer`; override it when the listing call already returns metadata.
  `None` means "unknown", never "missing". Never yield a name that is not
  one path component (`utils.is_safe_child_name()`: `""`, `.`, `..`, or one
  containing `/` or NUL): every URI scheme's listing skips such a name, the
  `UriPath` default included, and an override must too.
- `glob()` —
  pathlib semantics: hidden entries included, `**` never descends into
  directory symlinks (`recurse_symlinks=True` → `NotImplementedError`), a
  trailing `**` selects files too on 3.13+, a missing or non-directory base
  yields nothing, `""` → `ValueError`, an absolute pattern →
  `glob.NonRelativePatternError`. `recursive=None` enables recursion when a
  component is `**`; an explicit value wins. Validates eagerly, selects
  lazily. On a remote scheme a recursive glob lists every directory of the
  subtree, once. A path two `**` reach along several splits is yielded once
  (3.13+ `pathlib` yields it once per split). A literal component is checked
  as the running `pathlib` checks it: before 3.13 one that is not the last
  must be a directory (`a.txt/..` selects nothing), and from 3.12 the last
  is tested without following a link, so a dangling symlink is selected.
  An explicit `case_sensitive` compares literals against the listing too
  and yields the name as stored; `.` and `..` stay literal. A pattern with
  no component (`.`) is a `ValueError`; `rglob(".")` selects every entry
  below.
  - **`pattern=None`** expands the pattern THIS PATH CARRIES
    (`LocalPath("/etc/*.conf").glob(None)`), splitting at the first
    wildcard — the supported form for a path that is itself a pattern.
    `""` still raises.
  - **`on_error(error)`** is called when a directory cannot be listed,
    the same contract as `walk()`/`os.walk`: raising from it propagates,
    returning treats that directory as empty. Without it the listing is
    skipped in silence (pathlib's behaviour), so a caller could not tell
    an unreadable directory from an absent one. A base that is missing or
    not a directory is reported (once) whatever the first component is.
    `error.filename` names the directory even when the backend left it
    unset.
  - **`bound_loops=True`** skips a directory whose `(st_dev, st_ino)` is
    already on the CURRENT DESCENT PATH — a directory reachable below
    itself, which is what a loop is. It bounds a Windows junction loop,
    which `recurse_symlinks=False` cannot (a junction reports
    `is_symlink() == False`) and which `pathlib` itself walks until the
    recursion limit. One directory deliberately reachable under two
    SIBLING names (a shared layer junctioned in twice) is not a loop and
    both names expand — the rule is the ancestor chain, not everything
    seen, the same line `find -L` draws. A backend whose stat carries no
    identity (`MemPath`, most remote schemes, a `st_ino` of 0 or `None`) is
    walked unbounded.
  - **`native=True`** (default) follows the running interpreter on the two
    rules pathlib changed mid-series: a trailing `/` is ignored before 3.11
    and selects directories only from 3.11; `a**` raises `ValueError`
    before 3.13 and is a plain wildcard from 3.13. `native=False` applies
    one rule on every version (trailing `/` → directories only, `a**` → a
    plain wildcard), so a pattern answers the same on every interpreter and
    backend; `pathlib_next.testing`'s contract suite uses it.
- `rglob()` — `glob(f"**/{pattern}", recursive=True)`.
  `pattern=None` is `glob(None)`. `LocalPath` raises the audit events
  `pathlib.Path.glob` and `pathlib.Path.rglob` with the arguments `pathlib`
  passes on the running version (3.13+ also raises `glob` for the call
  `rglob` stands for); `pattern=None` raises none.
- `walk()` — drives
  `_scandir()`; its stats are trusted only with `follow_symlinks=False`.
  Without `follow_symlinks` a symlink to a directory and a Windows junction
  (`is_junction()`) are listed in `filenames` and not entered, as
  `pathlib.Path.walk()` does; with it they are entered, and nothing protects
  the walk from a loop (`LocalPath` included: its walk is this one).
  A listed name that is not one path component (`utils.is_safe_child_name()`)
  is left out of `dirnames`/`filenames`; `on_error`, when given, is called
  with a `ValueError` for it (`error.filename` is the directory).
- `touch()` — `FileExistsError` when `exist_ok=False`
  and the path exists; never truncates; creates with `open("x")` (falls
  back to `"w"` when `x` is unsupported); `chmod(mode)` only when `mode` is
  passed (unmasked); does not update an existing file's mtime.
  `LocalPath`/`FileUri` use pathlib's `touch()`.
- `_mkdir()` (stub) / `mkdir()`.
- `unlink()`, `rmdir()` — stubs.
- `rm()` — extension.
  `ignore_error` is a bool or `callable(error, path) -> bool` (True
  swallows); each error is offered once. Recursive removal is bottom-up and
  never descends through a directory symlink or a binding (a Windows
  junction, a mount point — `is_dir_binding()`): the entry itself is
  removed, never what is behind it, which is what `rm -r` does.
  `follow_symlinks=` (symlinks) and `follow_binds=` (bindings) choose per
  call: `False` (default, remove the entry), `True` (remove the contents
  behind it too, then the entry: a followed symlink is unlinked), `None`
  (leave it in place — the enclosing directory is then not empty and says
  so), or a callable `policy(path) -> bool | None` asked per entry, so one
  tree can keep one mount and follow another. The name matches
  `stat()`/`walk()`/`copy()`'s `follow_symlinks=` rather than a second
  vocabulary for the same idea. Path components before the final one are
  followed as usual; the path `rm()` is called on is decided by the same
  policy as one met on the way down (without `recursive` a symlink is only
  unlinked). A policy that is not `True`/`False`/`None`/a callable, or a
  callable's answer that is not one of the three, raises `ValueError`
  before anything is removed and is never offered to `ignore_error`. Every
  scheme that overrides `rm()` (`sftp:`, `dav:`, `s3:`, `gs:`, `az:`)
  takes and checks both keywords. `dav:` and the object stores hold no
  symlinks or bindings, so the keywords decide nothing there; `sftp:`
  uses its concurrent native walk (asyncssh backend) only for the default
  `follow_symlinks=False` and runs this walk otherwise. A listed name that is not one path component
  (`utils.is_safe_child_name()` with `windows=False`: the names come from
  the directory's own listing) is never joined onto it: it raises
  `ValueError`, offered to `ignore_error` as `(error, directory)` once per
  entry, and nothing is removed for it.
- `rename()` — stub. Implementations return the new path.
- `_symlink_to()` (stub; receives a path
  object) / `symlink_to()`
  — `force=True` unlinks an existing non-directory entry first (not atomic;
  never removes a directory). A `str` target goes through the overridable
  `_symlink_target()` and is stored verbatim; relative stays relative:
  `LocalPath` hands the text on untouched (`./t/` stays `./t/`, as
  `pathlib` stores it) except on Windows, which cannot resolve a relative
  target spelled with `/`, so the path class rewrites it there; `UriPath`
  does not parse it as a URI, and any other class normalizes it with
  `with_segments()`, which keeps per-instance state. Implemented by
  `LocalPath` and `SftpPath` only.
- `copy()`
  - A file copy calls `stat()` once on the source (type, mode, size and
    identity) and once on the target.
  - Existing target: `FileExistsError` unless `overwrite=True`; a directory
    target of a file copy → `IsADirectoryError`; a directory source needs
    `recursive=True`; copying into its own subtree → `OSError(EINVAL)`; onto
    the same file (or a case-insensitive alias) → `OSError(EINVAL)`. Two
    files of this machine (`LocalPath`, a subclass, `pathlib.Path`, a
    `file:` URI) are compared by identity whatever their classes, a
    directory reached through a symlink included. A type that answers
    `_node_key()` is compared by that, across classes. Otherwise "same" is
    `samefile()` where `stat()` carries `st_dev`/`st_ino`, else equal paths
    of one type that `_same_filesystem()` places together.
  - The source is opened before the target is touched; a failed stream
    removes the partial target. `overwrite=True` removes an existing file
    target with `unlink(missing_ok=True)` just before writing: a backend
    whose write replaces the file and which cannot delete implements
    `unlink()` so that `missing_ok=True` returns without removing.
  - A recursive copy refuses any child name that would not stay inside
    `target` — `..`, and when the target reads names with Windows rules
    (`utils.is_windows_flavoured()`) `\`, `:`, a trailing dot or space, and
    the reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`,
    `LPT1`-`LPT9`, with or without an extension). The names come from a
    listing the destination does not control (an archive, a remote index,
    an object-store key); on a Windows target `"C:x"` joins to a
    drive-relative path outside it and `"report."` is stored as `"report"`.
    Raised as `ValueError` through `ignore_error`, per child, like any other
    child failure.
  - `follow_symlinks=False` on a symlink recreates the link
    (`NotImplementedError` if either side cannot); only the target text is
    copied, not the link's own mode, times or owner. A dangling symlink at
    the target is written through, as `shutil.copyfile` does.
  - `preserve_metadata=True` copies permission bits only, and only a mode the
    source backend really reported (`FileStat.mode_known`): a file's, and a
    directory's after its children are copied, so a read-only directory
    still receives them. Between two different classes the setuid, setgid
    and sticky bits are dropped, so a mode read from a tar member or a
    remote server never makes a privileged file here.
  - `ignore_error`: `True` suppresses child errors of a recursive copy,
    `False`/`None` raise; a callable is called as `ignore_error(error)` and
    the error is **always** suppressed (its return value is ignored — not
    `rm()`'s `(error, path)` predicate).
  - `progress(path, bytes_copied, total_size | None)` per chunk of each file
    streamed; not called by `SftpPath`'s asyncssh recursive fan-out.
  - A `str` target goes through `_coerce_target()`: `with_segments()` by
    default, a URI parse on `UriPath` (so `copy("s3://b/k")` crosses
    schemes).
- `copy_into()` — `copy(target_dir / self.name, ...)`: the same
  keywords and defaults as `copy()`, and the new path is returned (pathlib
  3.14 returns it too). A `str` directory is a path on this backend, as for
  `copy()`. `ValueError` for a path with no name. Present on every backend
  and version; on 3.14 it replaces stdlib's, which overwrote and copied
  trees by default.
- `move_into()` — `move(target_dir /
  self.name, overwrite=overwrite)`, returning the new path.
- `replace()` — `move(target, overwrite=True)`, returning
  `target` as a path. A file replaces a file and a directory an empty
  directory; a directory that holds anything raises `OSError(ENOTEMPTY)`
  and a file onto a directory `IsADirectoryError`, both before anything is
  removed. `LocalPath` keeps stdlib's `os.replace()`.
- `move()` — validates first (missing source →
  `FileNotFoundError`, file onto directory → `IsADirectoryError`, existing
  target without `overwrite` → `FileExistsError`; a same-file spelling is
  renamed in place), and refuses before it removes anything a target that
  holds the source (`OSError(ENOTEMPTY)`), lies inside it
  (`OSError(EINVAL)`) or is a link to the same file (`OSError(EINVAL)`).
  Sameness is decided as for `copy()`. Two hard links of one file are two
  names: an existing target needs `overwrite=True`, and the source name is
  then removed, so the target name keeps the content. Tries `rename()`
  when `_rename_compatible(target)` (default: only onto a path of the very
  same class; `UriPath` leaves it to `rename()`, which raises
  `NotImplementedError` for a target it cannot reach; `LocalPath` requires a
  local target), falling back to `copy(recursive=True)` + `rm`/`unlink` on
  `NotImplementedError` or `OSError(EXDEV)`. The fallback recreates a
  symlink, and the links inside a moved tree, at a target whose class
  implements `_symlink_to()`, so the result does not depend on whether
  `rename()` was available; any other target receives what the links point
  at. `overwrite=True` replaces a
  local file atomically (`replace()`); elsewhere the target is unlinked just
  before the rename. Returns `rename()`'s result (`None` on the fallback).

### `FsPathLike`, `PathLike`, `PurePathLike`

```python
class FsPathLike(Protocol):
    def __fspath__(self) -> str: ...
```

`Protocol` with `__fspath__()`.
**`PathLike`** = `str | Path`; **`PurePathLike`** = `str | Pathname`.

## Local filesystem (`pathlib_next.fspath`)

### `LocalPath`

```python
class LocalPath(pathlib.Path, Path):
    def stat(self, *, follow_symlinks=True): ...
    def chmod(self, mode, *, follow_symlinks=True): ...
    def is_dir(self, *, follow_symlinks=True): ...
    def is_file(self, *, follow_symlinks=True): ...
    def exists(self, *, follow_symlinks=True): ...
    def read_text(self, encoding=None, errors=None, newline=None) -> str: ...
    def write_text(self, data, encoding=None, errors=None, newline=None): ...
    def _scandir(self): ...
    def _chown(self, uid, gid, *, follow_symlinks=True) -> None: ...
    #  Path's signatures: glob, rglob, walk, symlink_to, _symlink_to, copy,
    #  copy_into, move, move_into, rm
```

`pathlib.WindowsPath`/`PosixPath` (by `os.name`) with
`Path` mixed in; stdlib behavior except where overridden: `_scandir()`
(tuples from `os.scandir` lstat), `walk()`, `glob()`,
`stat()`/`chmod()` (`follow_symlinks=` on 3.9; `chmod` accepts octal
strings), `is_dir()`/`is_file()` (`follow_symlinks=` before 3.13),
`_symlink_to()`, `_chown()` (`shutil.chown`; `NotImplementedError` where
`os.chown` is missing, i.e. Windows), plus pathlib_next's `copy`, `move`,
`copy_into`, `move_into`, `exists`, `rglob`, `read_text`, `write_text` and
`symlink_to`.
`exists()` returns `False` for any `OSError`/`ValueError` on every Python
version; `is_dir()`, `is_file()`, `is_fifo()`, `is_socket()`,
`is_block_device()` and `is_char_device()` are stdlib's: before 3.13 they
raise for an error other than ENOENT, ENOTDIR, EBADF or ELOOP (a
`PermissionError` from `stat()`, say), from 3.13 they return `False`. A
stdlib `pathlib.Path` is not a `pathlib_next.Path`, and `MemPath`/`Uri`/
`UriPath` are not stdlib paths.

### `PosixPathname`, `WindowsPathname`

```python
class PosixPathname(PurePosixPath, Pathname): ...
class WindowsPathname(PureWindowsPath, Pathname): ...
```

pure classes over
`PurePosixPath`/`PureWindowsPath` implementing `Pathname`.

## In-memory filesystem (`pathlib_next.mempath`)

### `MemPath`

```python
class MemPath(Path):
    def __init__(self, *segments, backend=None): ...
    backend
```

`Path` over nested dicts; the
reference `Path` subclass. Segments may be `str`, `Pathname` or `MemPath`
(the first `MemPath` argument names the backend, as the left operand does
for `/` and `joinpath()`, and an explicit `backend=` wins over it; another
`Path` → `NotImplementedError`). Joined and normalized like `PurePosixPath`.
An unknown keyword → `TypeError`.

- `backend` (a `MemPathBackend`); `segments` is a tuple (`("", "a")` for
  `/a`); `parts` is `(segments, backend)`; `as_uri()` →
  `mempath:<quoted posix path>` (no `mempath:` scheme is registered; build
  `MemPath` directly).
- `..` is applied to the tree, as on a POSIX filesystem: the directory it
  leaves must exist (`/missing/../a` → `FileNotFoundError`, `/f/../a` with
  `f` a file → `NotADirectoryError`), and above the root it stays at the
  root.
- `stat()` → `FileStat` with `st_size` and `st_mtime` (time of the last
  write); the mode is a placeholder (`mode_known=False`).
- `open()` supports `r`, `w`, `x`, `a` (binary or text); `+` modes →
  `NotImplementedError`. Writes are visible after `flush()`/`close()`; two
  `a` handles on one file both land (each adds only what it wrote).
- `rmdir()` of the root → `OSError(EBUSY)`, so `root.rm(recursive=True)`
  empties the tree and then raises that.
- Not implemented (`NotImplementedError`): `relative_to()`,
  `rename()` (`move()` copies), `chmod()`, `symlink_to()`.
- A `str` destination to `copy()`/`move()` stays on the same backend.

### `MemPathBackend`

```python
class MemPathBackend(dict): ...
```

Storage: `dict` value = directory,
`bytearray` (`MemFile`, carrying `mtime`, which `copy`, `deepcopy` and
`pickle` keep on every version) = file. Pass one instance as `backend=` to
share a tree; each root `MemPath()` otherwise gets its own.

## Protocols (`pathlib_next.protocols`)

### `fs.FileStatLike`

```python
class FileStatLike(Protocol):
    st_mode: int
    st_size: int
    st_mtime: float
```

`st_mode`, `st_size`, `st_mtime`.

### `fs.Stat`

```python
class Stat(Protocol):
    def stat(self, *, follow_symlinks=True) -> FileStatLike: ...
    def lstat(self) -> FileStatLike: ...
    def exists(self, *, follow_symlinks=True): ...
    def is_dir(self, *, follow_symlinks=True): ...
    def is_file(self, *, follow_symlinks=True): ...
    def is_symlink(self): ...
    def is_block_device(self): ...
    def is_char_device(self): ...
    def is_fifo(self): ...
    def is_socket(self): ...
```

`stat()` (stub). Derives `lstat()`,
`exists()`, `is_dir()`,
`is_file()`, `is_symlink()`, `is_block_device()`,
`is_char_device()`, `is_fifo()`, `is_socket()`; any `OSError`/`ValueError`
from `stat()` reads as `False`.

### `fs.Chmod`

```python
class Chmod(Protocol):
    def chmod(self, mode, *, follow_symlinks=True): ...
    def lchmod(self, mode): ...
    def chown(self, uid=None, gid=None, *, follow_symlinks=True) -> None: ...
    def _chown(self, uid, gid, *, follow_symlinks=True) -> None: ...
```

`chmod()` (stub; every
implementation normalizes through `utils.as_mode()`, so `"0755"` works),
`lchmod()`, `_chown()` (stub, receives a
canonical pair) / `chown()` —
`None` or `-1` leaves a field unchanged, `int` is an id, `str` a name; an
all-unchanged call returns without touching the backend.

### `io.BinaryOpen`

```python
class BinaryOpen(Protocol):
    def open(
        self, mode="r", buffering=-1, encoding=None, errors=None,
        newline=None,
    ) -> _io.IOBase: ...
    def read_bytes(self) -> bytes: ...
    def read_text(self, encoding=None, errors=None, newline=None) -> str: ...
    def write_bytes(self, data): ...
    def write_text(self, data, encoding=None, errors=None, newline=None): ...
    def copy(self, target, *, progress=None, chunk_size=...): ...
    def _open(self, mode="r", buffering=-1) -> _io.IOBase: ...
```

`_open()` (stub).
`open()`
validates the mode like builtin `open()` (`ValueError`) and passes
`_open()` a canonical `r`/`w`/`x`/`a` plus optional `+` (never `b`/`t`);
text mode wraps in `TextIOWrapper` and closes the handle if wrapping fails.
An `_open()` that cannot honor a mode raises `NotImplementedError`. Derives
`read_bytes()`, `read_text()`,
`write_bytes(data)`, `write_text()`, `copy()` (`chunk_size` defaults to `shutil.COPY_BUFSIZE`; `progress(bytes_copied, total_size | None)`; an empty file reports once).

### `checksum.NativeChecksum`

```python
class NativeChecksum(Protocol):
    def checksum(self, algorithm="md5") -> str: ...
    def supported_checksums(self) -> FrozenSet[str]: ...
```

Opt-in (not on `Path`):
`checksum()` MUST raise `NotImplementedError` whenever
it cannot return a genuine content digest under exactly that algorithm
(never a different algorithm, never an ETag-like value); callers catch only
`NotImplementedError`. `supported_checksums()` (default
empty) is advisory and never raises.

## Utilities (`pathlib_next.utils`)

### `glob` (`pathlib_next.utils.glob`)

```python
def glob(
    path, *, dironly=False, root_dir=None, recursive=False,
    include_hidden=False, case_sensitive=None, native=True, on_error=None,
    bound_loops=False,
) -> Iterator[_Globable]: ...
def parse_pattern(pattern, *, native=True) -> Tuple[List[str], bool]: ...
def select(
    base, parts, *, dironly=False, recursive=True, include_hidden=True,
    case_sensitive=None, native=True, on_error=None, bound_loops=False,
) -> Iterator[_Globable]: ...
def full_match(segments, pattern, case_sensitive) -> bool: ...

RECURSIVE = '**'

class NonRelativePatternError(NotImplementedError, ValueError): ...
```

`glob.glob()`: the pattern
is itself a path (`UriPath("file:/x/**/*.py")`); like stdlib `glob`, hidden
names need `include_hidden=True`. It is split at the first wildcard, never
in the path's anchor (a drive, `\\?\C:\`); a bad pattern raises when it is
called, the selection is lazy. `glob.parse_pattern()` (`ValueError`/`NonRelativePatternError`),
`glob.select()` (the engine behind
`Path.glob()`), `glob.full_match()`,
`glob.NonRelativePatternError(NotImplementedError, ValueError)`,
`glob.RECURSIVE = "**"`.

### `sync` (`pathlib_next.utils.sync`)

```python
class PathSyncer:
    def __init__(
        self, checksum=None, /, remove_missing=False, follow_symlinks=True,
        symlink_mode="preserve", hook=None, ignore_error=False,
        quick_check=True,
    ) -> None: ...
    def sync(self, source, target, /, dry_run=False, ignore_error=None): ...
    def log(self, msg, *args): ...
```

One-way tree sync between any two
`Path` implementations.

- `.sync()` — `None` uses
  the constructor policy; a bool or callable overrides it for this call.
- `checksum=None`: a native digest when both sides share an algorithm
  (`NativeChecksum`), else a streamed md5 on both sides; a callable
  `checksum(entry: PathAndStat)` is compared with `==`.
- `quick_check=True`: when either side is non-local (`is_local()`), equal
  `st_size` and `st_mtime` skip the checksum; `st_mtime == 0` never matches;
  a mismatch still checksums.
- `hook(source: PathAndStat, target: PathAndStat, event: SyncEvent,
  dry_run: bool)` is called for every event (structural ones included) with
  the call's `dry_run`.
- `ignore_error`: bool or `callable(error, source, target, event) -> bool`
  (`source` and `target` are `PathAndStat`), offered once per error; a
  tolerated error is logged at WARNING on logger
  `pathlib_next.sync` and reported to `hook` as `SyncEvent.Error`.
  `.log(msg, *args)` (INFO on the same logger) is overridable by a subclass.
- Safety, all through `ignore_error`: a missing root `source` →
  `FileNotFoundError`; overlapping `source`/`target` (one inside the other,
  or two names of one file: decided like `copy()`/`move()` decide "same
  file", so two files of this machine whatever their classes, two spellings
  of one `MemPath` or archive node, and else one type that
  `_same_filesystem()` places together) → `ValueError`; a child
  name that would leave `target` (`..`, or on a Windows target `\`, `:`, a
  trailing dot or space, a device name) → `ValueError`, decided on the name
  as listed, in the source's listing and the target's alike; a symlink
  inside `target` is replaced, never followed, and so is a Windows
  junction below its root (event `TypeMismatch`; `rmdir()` removes the
  junction and nothing behind it). A mount point inside `target` is part
  of the tree and is synced into; the root `target` is used as given.
  Listing entries with unknown
  stats are re-stat'd. A name the source lists is never "missing": a listed
  link that cannot be resolved (dangling, a loop, an unmounted volume) is an
  error for that entry (`FileNotFoundError` or the stat's `OSError`, event
  `SyncStart`) and the same-named target entry is left alone, whatever
  `remove_missing` is; only a name the listing no longer holds is removed.
- Cost: each root is stat'd once and each directory of each side is listed
  once; the listing's stat answers for every entry that is not a link, so a
  sync that changes nothing makes no other `stat()` call on a backend whose
  `_scandir()` returns stats. A link, an entry whose listing stat is `None`
  and a target name the listing did not report are stat'd, one call each. A
  backend with no `_scandir()` of its own pays the default's `stat()` per
  entry of every directory it lists.
- `remove_missing=True` deletes target entries absent from the source. With
  `False`, a non-empty target directory whose source became a file or link
  is kept (`IsADirectoryError`, event `TypeMismatch`).
- `follow_symlinks=False` + `symlink_mode="preserve"` recreates source links
  with the raw `readlink()` text (target must implement `symlink_to()`, else
  `NotImplementedError` through `ignore_error`, in a dry run too);
  `"reject"` raises `NotImplementedError`.
- Changed files are written to a hidden temporary sibling
  (`.NAME.<12 hex>.pathlib-next-tmp`, NAME cut to keep the whole within 255
  bytes) and renamed over the target where the target supports `rename()`;
  where `rename()` refuses an existing target the old file is removed first,
  and if the second rename fails the new version is kept under its
  temporary name and the error (an `OSError`) names it. A name of exactly
  that form is the library's own: never a source, never removed as
  "missing"; a stale one (not being written by this process, modification
  time known and over a day old) is removed from a target directory when a
  changed file or link in it is next written, and by `remove_missing=True`
  (event `RemovedMissing`). FIFOs, sockets and devices are skipped. A dry run makes the same
  decisions without changes.

#### `SyncEvent`

```python
class SyncEvent(Enum):
    Copy = ...
    RemovedMissing = ...
    Synced = ...
    CreatedDirectory = ...
    SyncStart = ...
    TypeMismatch = ...
    CheckTargetChild = ...
    CheckTargetChildren = ...
    SyncChild = ...
    SyncChildren = ...
    Symlink = ...
    Compare = ...
    Skipped = ...
    Error = ...
```

Members: `Copy`, `RemovedMissing`, `Synced`,
`CreatedDirectory`, `SyncStart`, `TypeMismatch`, `CheckTargetChild`,
`CheckTargetChildren`, `SyncChild`, `SyncChildren`, `Symlink`, `Compare`
(a comparison failed; only passed to `ignore_error`), `Skipped` (not a
file, directory or link), `Error` (an error was tolerated).

#### `PathAndStat`

```python
class PathAndStat:
    def __init__(self, path, *, follow_symlink=True) -> None: ...
    @classmethod
    def from_stat(cls, path, stat) -> PathAndStat: ...
    def exists(self): ...
    def refresh(self, follow_symlink=True): ...
```

`path` plus cached
`stat` (`FileStat | None`); `from_stat(path, stat)`, `exists()`,
`refresh(follow_symlink=True)`; `is_*` attributes delegate to the stat
(always-`False` callables when missing).

### `stat` (`pathlib_next.utils.stat`)

```python
class FileStat:
    def __init__(self, st_mode=None, st_size=0, st_mtime=0, is_dir=False): ...
    @classmethod
    def from_stat(cls, stat) -> FileStat: ...
    @classmethod
    def from_path(cls, path, *, follow_symlink=True): ...
    def settime(self, value): ...
    def setmode(self, value, isdir=None): ...
    def items(self): ...
```

Slotted stat for non-`os` backends. Without `st_mode`, a placeholder
(`S_IFREG|0o444` / `S_IFDIR|0o555`) with `mode_known=False`. A bare
permission `st_mode` (`0o644`) gets the type bits of `is_dir` ORed in and
counts as reported; one that carries a type keeps it.
`from_stat(stat)` (a `FileStat` passes through; `None` fields become 0),
`from_path(path, *, follow_symlink=True) -> FileStat | None` (`None` only
on `FileNotFoundError`), `settime(value)`, `setmode(value, isdir=None)`,
`items()`. `is_dir()`/`is_file()`/... are **methods**.

### `checksum` (`pathlib_next.utils.checksum`)

```python
def md5(path, chunk_size=65536) -> str: ...
def sha256(path, chunk_size=65536) -> str: ...
def stream(path, algorithm="md5", chunk_size=65536) -> str: ...
def native(path, algorithm="md5") -> str | None: ...
```

`md5()`, `sha256()`, `stream()`
(streamed through `open("rb")`, `usedforsecurity=False`),
`native()` (`None` when the protocol is
missing or raises `NotImplementedError`). `md5`/`sha256` are also
importable from `pathlib_next.utils`.

### `archive` (`pathlib_next.utils.archive`)

```python
def make_archive(src, format, target) -> None: ...
def unpack_archive(archive, dest) -> None: ...
```

`make_archive()` (`format` `"zip"` or
`"tar"`, else `ValueError`; `src` file or directory of any `Path`; built in
a temporary buffer, written to `target` only when complete; zip64 always;
every directory is a member, empty ones too; a member carries the source's
modification time and, in a tar, its permission bits — a source that
reports no time gets 1980-01-01 (zip) or the epoch (tar), and one that
reports no mode gets 0o644 for a file and 0o755 for a directory; a tar
member is sized from the bytes read, not from `stat()`)
and `unpack_archive()` (format from the name, else magic
bytes; creates `dest`; non-seekable streams are buffered; members that
would leave `dest` are skipped, and for a Windows-flavoured `dest` so are
members with a part Windows would rewrite or send to a device
(`UserWarning`); tar hard links and links to regular files are extracted as
copies, other links — to nothing, to a directory, or in a loop — skipped
with `UserWarning`). Also importable from `pathlib_next.utils`.

### Helpers (`pathlib_next.utils`)

```python
def is_safe_child_name(name, *, windows=False) -> bool: ...
def is_windows_flavoured(path) -> bool: ...
def as_mode(mode) -> int: ...
def as_owner(uid, gid) -> Tuple[Optional[int], Optional[int]]: ...
def as_error_handler(
    ignore_error, *, default=False,
) -> Callable[..., bool]: ...
def notimplemented(method): ...
def sizeof_fmt(num) -> str: ...
def parsedate(date): ...

class LRU:
    def __init__(self, func, maxsize=128, on_evict=None): ...
    def discard(self, *args) -> bool: ...
    def invalidate(self, *args) -> V: ...
    maxsize
UNCHANGED = None
```

- **`is_safe_child_name()`** — `False` for a
  non-`str`, `""`, `.`, `..`, or a name containing `/` or NUL; with
  `windows=True` also `\`, `:`, a trailing dot or space, and the reserved
  device names `CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9` (any
  case, with or without an extension: `nul.txt`).
  **`is_windows_flavoured()`**
  — `True` for a `PureWindowsPath` or a path whose `filepath` is one.
- **`LRU`** — thread-safe memoizing cache,
  called like `func`. `on_evict(key_tuple, value)` runs for every dropped
  value (overflow, `maxsize` shrink, `invalidate`/`discard`, a losing
  concurrent miss), outside the lock, exceptions suppressed. `discard(*args)
  -> bool`, `invalidate(*args)` (discard + recompute), settable `maxsize`.
  `maxsize=None` is unbounded; `maxsize=0` (or less) stores nothing, so every
  call runs `func` and its result never reaches `on_evict`.
- **`as_mode()`** — `int` passes through; `str` is octal (optional
  `0o`), any non-octal digit → `ValueError`. A `bool` → `TypeError`; a number
  below 0 or above `0o177777` (the 16 bits of a `st_mode`, type bits
  included) → `ValueError`.
- **`as_owner()`** — `-1` → `None`;
  `str` names pass through; a `bool` → `TypeError`, an id outside
  0..2**32-1 → `ValueError`. **`UNCHANGED = None`**.
- **`as_error_handler()`** — a
  callable passes through untouched (arity is the call site's: `rm` `(error,
  path)`, `copy` `(error)`, `PathSyncer` `(error, source, target, event)`);
  a bool (or `None` → `default`) becomes a constant-returning callable.
- **`notimplemented()`** — decorator; calling raises
  `NotImplementedError("Method not implemented: <name>")`.
- **`sizeof_fmt()`** — `1536` → `"1.5K"`.
- **`parsedate()`** — UTC epoch seconds from an HTTP date
  string (zone offset applied, none = UTC), a `struct_time`/tuple (UTC minus
  any offset), or a number (returned unchanged); `None` or an unparseable
  string → `0`. `bytes`, a `datetime` and a tuple of fewer than six items are
  not accepted (`ValueError`/`TypeError`), and a date that does not exist
  (31 Feb) rolls over instead of being refused.

## Testing helpers (`pathlib_next.testing`, needs `pytest`)

### `FIXTURE_TREE`

```python
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
```

`{"a.txt": "a", "b.py": "b", ".hidden.txt": "hidden",
"sub": None, "sub/c.py": "c", "sub/nested": None, "sub/nested/d.py": "d",
"empty_dir": None}` (`None` = directory).

### `populate_fixture_tree`

```python
def populate_fixture_tree(root): ...
```

Builds `FIXTURE_TREE` under an
existing empty directory through the path's own `mkdir()`/`write_text()`
(any `Path`, or a stdlib `pathlib.Path`).

### `PurePathContract`

```python
class PurePathContract: ...
```

Name/suffix/stem, parents, join, `match()`; needs a
`root` fixture only.

### `ReadPathContract`

```python
class ReadPathContract(PurePathContract):
    supports_listing = True
    supports_empty_directories = True
    distinguishes_file_types = True
    supports_pickle = True
```

Exists/types, reads and read
modes, partial `read(n)`, `iterdir()`, `stat()` (the size of a longer file, a
directory told from a file, a numeric `st_mtime`), `_scandir()` agreeing with
`stat()` for every child, nothing existing below a file, `glob()`/`rglob()`,
`walk()`, with pathlib's exception types, and a pickle round trip that keeps
`==` and `hash()`. Capability attributes: `supports_listing`,
`supports_empty_directories`, `distinguishes_file_types`, `supports_pickle`.

### `PathContract`

```python
class PathContract(ReadPathContract):
    supports_rename = True
    supports_append = True
    supports_exclusive_create = True
    enforces_directory_hierarchy = True
    supports_mkdir = True
    supports_delete = True
    supports_move = True
    supports_unusual_names = True
    supports_question_mark_names = False
```

`mkdir()`, writes and write/append/
exclusive modes, a payload of about 290 KiB holding every byte value and
runs of CR/LF written whole and in pieces and read back whole and in parts,
names holding a space, `#`, `%`, `+`, `&`, `=` and mixed case written,
listed, renamed and removed as written, `unlink()`, `rmdir()`, `rm()` (also
with `follow_symlinks=`/`follow_binds=`: an implementation that overrides
`rm()` must accept both), `copy()` (recursive), `move()`, `rename()` (also
onto a non-empty directory, which must raise), `touch()`, copying a file
onto a separately built spelling of itself (`OSError`, content kept) and
`_same_filesystem()` within one root. Capability attributes:
`supports_rename`, `supports_append`, `supports_exclusive_create`,
`enforces_directory_hierarchy` (also covers rename onto a directory),
`supports_mkdir` (every test that creates a directory), `supports_delete`
(`unlink()`, `rmdir()`, `rm()`, and the overwriting half of `copy()`),
`supports_move` (`move()`, by `rename()` or copy and delete),
`supports_unusual_names`, and `supports_question_mark_names` (names holding
`?`; **False by default**, because a Windows file system cannot store one:
set it `True` for a URI scheme or a remote store).

- Both I/O contracts need `root` to be a **fresh, function-scoped** directory
  populated with `populate_fixture_tree()` (which uses `mkdir()` and
  `write_text()`: a backend without them seeds `root` another way); two
  contract classes must not share one. Capability attributes default to
  `True` except `supports_question_mark_names`; setting one `False` makes the
  tests it covers skip. `DIRECTORY_ERRORS = (IsADirectoryError,
  PermissionError)` and `NOT_EMPTY_ERRNOS = (ENOTEMPTY, EEXIST)` are the
  accepted platform variants.

```python
import pytest
from pathlib_next import LocalPath as MyPath  # your Path subclass
from pathlib_next.testing import PathContract, populate_fixture_tree

class TestMyPath(PathContract):
    @pytest.fixture
    def root(self, tmp_path):
        return populate_fixture_tree(MyPath(tmp_path))
```

## URIs (`pathlib_next.uri`, `uri` extra)

The URI layer (this section, `Built-in schemes` and the URI-layer parts of the
closing sections) builds on `Path` and `Pathname` above and needs the `uri` extra.

### `Uri`

```python
class Uri(Pathname):
    def __init__(self, *uris, **options): ...
    source: Source
    path: str
    query: str
    fragment: str
    parts
    normalized_path
    segments
    parent
    def as_uri(self, sanitize=False): ...
    def with_source(self, source): ...
    def with_path(self, path): ...
    def with_segments(self, *segments): ...
    def with_query(self, query): ...
    def with_fragment(self, fragment): ...
    def with_name(self, name) -> Self: ...
    def with_suffix(self, suffix) -> Self: ...
    def with_stem(self, stem) -> Self: ...
    def is_absolute(self): ...
    def is_relative_to(self, other): ...
    def relative_to(self, other, *, walk_up=False): ...
    def is_local(self): ...
    def as_posix(self): ...
    def __fspath__(self): ...
    def host_fspath(self) -> str: ...
```

Pure RFC 3986 URI, parsed lazily. Arguments
(`str`, `bytes`, `Uri`, `pathlib`/`pathlib_next` paths, `os.PathLike`;
`None` and `""` are the empty URI, anything else is a `TypeError` naming its
type) join right to left like `joinpath` (an absolute one restarts); this is not RFC
3986 reference resolution (`Uri("http://h/d") / "x"` is `http://h/d/x`),
but the dot segments of the joined path are removed as RFC 3986 5.2.4
says: `Uri("http://h/d/", "../x")`, `Uri("http://h/d/") / "../x"`,
`Uri("http://h/d/") / Uri("../x")` and `Uri("http://h/d/../x")` are all
`http://h/x`, and `Uri("http://h/d/x") / ".."` is `http://h/d/`. A `..`
that would pass the root of an absolute path is dropped; a relative path
keeps its leading `..` (`Uri("a") / "../../x"` is `../x`). A
percent-encoded dot segment (`%2e%2e`) is removed after decoding. An
absolute local path **object** (`pathlib.Path`, `LocalPath`) becomes
`file:`; a relative one joins like a `PurePath`. A `str` argument is always
URI text: wrap a path string (`LocalPath(s)`), because `UriPath("/abs/x")`
is a path with no scheme whose I/O raises `NotImplementedError` and
`UriPath("C:/Temp/x")` has the scheme `c`.

- Properties: `source -> Source`, `path -> str` (percent-decoded),
  `query -> Query` (**percent-encoded as received**, sent unchanged; empty
  when there is none), `fragment -> str` (`""` when there is none),
  `parts -> (source, path, query, fragment)` (not path
  segments; use `segments`), `normalized_path`, `segments`, `parent`
  (`http://h/a` → `http://h/`; a trailing `/` is kept, so
  `Uri("http://h/d/").name == ""`).
- Methods: `as_uri(sanitize=False)`, `with_source()`, `with_path()`,
  `with_segments()`, `with_query(str | mapping | pairs)` (a `str` is taken as
  already encoded), `with_fragment()`; `with_name`/`with_suffix`/`with_stem`
  keep query and fragment. `with_path(path)` gives a relative path under an
  authority the leading `/` the constructor gives it; `with_segments(*segments)`
  joins the spelling `segments` returns (a leading `""` is the root) with `/`,
  and an element may be `str`, `bytes`, a `PurePath` or an `os.PathLike`.
  `is_absolute()` (path starts with `/`),
  `is_relative_to(other)`, `relative_to(other, *, walk_up=False)`
  (`s3://b`/`http://h` count as the root), `is_local()`
  (`Source.is_local()`), `as_posix()` (`user@host:path` when a host is
  present).
- `str()`/`repr()` drop the password (`sftp://u:pw@h/p` → `sftp://u@h/p`);
  `as_uri(sanitize=False)` keeps it. Non-ASCII hosts render as IDNA. A
  query or fragment is printed as it is: a token carried there shows in
  `str()`/`repr()`.
- Authority: the first unescaped `:` of the userinfo separates user from
  password (`us%3Aer:pw` is the user `us:er`); a host of digits only
  (`s3://20240101/key`) is the host; a port is 0-65535 (`ValueError`
  otherwise, as for a `:` followed by anything but digits); an IPv6 zone is
  written `[fe80::1%25eth0]` (a bare `%` is read too). A name that holds a
  lone surrogate (a Windows file name can) renders as its UTF-8 bytes, so
  `str()`, `hash()` and `==` work on it.
- `==`/`hash` use the URI text; equal to another `Uri` or a URI string,
  never to a non-URI `Pathname`.
- `__fspath__()` — the path for a `file:` URI on this machine (a named host
  on Windows is a UNC path) or for a scheme with `_host_filesystem_path =
  True` (`sftp:`, whose path is meaningful on **its** host); otherwise
  `NotImplementedError`. `host_fspath()` — the latter only, never local.

### `UriPath`

```python
class UriPath(Uri, Path):
    def __init__(
        self, *args, schemesmap=None, findclass=False, **kwargs,
    ) -> UriPath: ...
    backend
    def with_backend(self, backend): ...
    def _initbackend(self): ...
    def _listdir(self) -> Iterator[str]: ...
    def _scandir(self) -> Iterator[Tuple[str, Optional[FileStat]]]: ...
    def _reduce_options(self) -> dict: ...
```

`Uri` + `Path`. The bare class (or `findclass=True`)
returns the subclass registered for the scheme, or plain `UriPath` for an
unknown scheme (its I/O raises `NotImplementedError`). Resolution: classes
already imported → entry point in group `pathlib_next.schemes` → built-in
`pathlib_next.uri.schemes.*` module. An explicit `schemesmap` is the only
map consulted. It is an allow-list of classes for dispatch, not a sandbox:
the path keeps it and every class chosen after construction uses it -- a
join with a `Uri`/`UriPath` argument, `with_source()`, a `copy()`/`move()`
`str` destination and the outer URI of a `zip:`/`tar:`/`archive:` path (a
scheme the map omits gives a plain `UriPath`, whose I/O raises
`NotImplementedError`) -- but it restricts nothing an already-built path or
class does.

- Registering: `__SCHEMES = ("myscheme",)` in the class body (name-mangled:
  redeclare per class; the class name must not start with `_`). Defining or
  importing the subclass is enough, including after the first dispatch.
- `backend` — per-instance connection state from `_initbackend()` (base:
  `None`), built on first use. A derived path (`/`, `joinpath()`,
  `parent`, `parents`, `with_name()`, `with_suffix()`, `with_query()`,
  `relative_to()`, `UriPath(base, x)`, a `Uri`/`PurePath` join) never
  builds one -- so it never imports an I/O library either -- and takes the
  backend its source path already holds, or none. Paths derived from one
  another on one endpoint share one slot for the backend they derive for
  themselves: the first of them to need one calls `_initbackend()` (under a
  lock, so two threads build one) and the rest then use that object, so 20
  children of a fresh root open one connection. A path built separately
  (`UriPath(url)` twice), one on another endpoint, the sourceless result of
  `relative_to()` and a path that holds a supplied backend are not in it.
  `with_backend(backend)` returns a copy using `backend`. A backend is only
  shared within one endpoint (scheme, userinfo, host, port): a join,
  `with_source()` or `UriPath(base, url)` onto another endpoint takes none
  and builds a fresh one on first use, so credentials and sessions never
  follow. When several joined paths hold one, the rightmost whose endpoint
  is the result's is taken; the sourceless result of `relative_to()` holds
  none, so `dst_root / src.relative_to(src_root)` uses `dst_root`'s.
  `_same_filesystem()`: two URIs are on different filesystems only when
  BOTH carry a backend the caller supplied (`backend=`, `with_backend()`,
  or inherited from such a path) and those are different objects -- two
  connections, or fakes standing in for two hosts. A backend a path built
  for itself counts as none, so two separately built paths to one URL are
  the same file. The path records that it built the backend and passes the
  record on, so this holds for any backend object, a `dict` or a slotted
  class without `__weakref__` included. A scheme that hands its own backend
  on through `backend=` (as a `with_source()` override does) is recognised
  as derived when the object is weakly referenceable (every built-in
  `Base*Backend` is) and `_initbackend()` built it.
  Copies: `pickle` holds the URI text (userinfo included, since that is the
  URI) and the `schemesmap`, and nothing else: no session headers, client
  kwargs or live client. A supplied backend goes along only if its class
  sets `picklable = True`; a derived one is built again on demand.
  `copy.copy()` and `copy.deepcopy()` share the backend object, supplied or
  derived, and never duplicate a connection. None of them carries the stat
  hint. `==`, `hash()`, `_same_filesystem()` and `_node_key()` answer as
  before, except that a pickle which dropped a supplied backend leaves a path
  with none, which `_same_filesystem()` cannot tell from another host's.
  A subclass with per-path state that is not a secret returns it from
  `_reduce_options()` (constructor keywords).
- Listing: implement `_listdir() -> Iterator[str]` or override
  `_scandir()`; `iterdir()` wraps each name with the entry's stat as a
  single-use hint (the child's first `stat()` returns it, later calls
  re-fetch). The default `_scandir()` skips a listed name that is not one
  path component; an override must do the same.
- `/` and `joinpath()` choose the result class from the scheme.
- `rename()`/`symlink_to()` take a `str` as an already-decoded path (`?`,
  `#`, `%`, `:` are filename characters); a relative `rename()` target is a
  sibling of `self`. A target on another endpoint (or another archive or
  Azure container) raises `NotImplementedError`, so `move()` copies and
  deletes.
- `copy()`/`move()` read a `str` destination **by its shape**: with a
  scheme (`s3://bucket/key`, `data:,abc`) it is a URI, so a cross-scheme
  copy works; without one it is a decoded path on this endpoint — absolute
  replaces the path, relative is a sibling, as `rename()` resolves it — and
  keeps this path's source and backend rather than opening a second
  connection. A one-letter scheme is a Windows drive, so `C:/Temp/x` is a
  path.

### `Source` (`pathlib_next.uri.source`)

```python
class Source(NamedTuple):
    def __init__(self, scheme, userinfo, host, port): ...
    def as_str(self, sanitize=True) -> str: ...
    @classmethod
    def from_str(cls, source, strict=True): ...
    def parsed_userinfo(self) -> tuple[str, str]: ...
    def get_scheme_cls(self, schemesmap=None): ...
    def is_local(self) -> bool: ...
```

`NamedTuple`,
falsy when all fields are empty; indexes by position, slice or field name.
`as_str(sanitize=True)` is the composer `Uri.as_uri()` uses (`file:` for an
empty host; an invalid scheme or port is a `ValueError`); `str()`/`repr()`
redact the password, and the whole userinfo for `github:`/`gitlab:`/`git:`
(the fields keep it). `Source.from_str(source, strict=True)` reads the
authority as `Uri` does (`ValueError` for a path/query/fragment when
strict, naming the component and never the input),
`parsed_userinfo() -> (user, password)` (`""` when absent; `userinfo`
stays the decoded `user:password` text),
`get_scheme_cls(schemesmap=None) -> type[UriPath]`, `is_local()` —
`localhost`/empty host or an address of this machine (IP literal, or any
A/AAAA answer via `netimps`); the answer depends on the host alone and is
kept for 60 seconds for at most 256 hosts, so a miss does DNS.

### `Query` (`pathlib_next.uri.query`)

```python
class Query(str):
    def __init__(self, query, *, encoding="utf-8", separator="&"): ...
    def decode(self) -> list[tuple[str, str | None]]: ...
    def to_dict(self, *, single=False): ...
```

`str` subclass holding the encoded query; built from a `str` (taken as
encoded and kept byte for byte), a mapping (a sequence value repeats the
key) or `(key, value)` pairs, which are encoded so that `decode()` returns
what was given: `+`, `=`, the separator, `#`, `%` and white space are
escaped (`{"sig": "ab+cd=="}` is `sig=ab%2Bcd%3D%3D`).
`decode() -> list[tuple[str, str | None]]` follows RFC 3986, not
form-urlencoding: only `%XX` escapes are decoded and a `+` stays a plus
(`urllib.parse.parse_qsl` reads form text). Iteration yields the decoded
pairs, `to_dict(*, single=False)`.

## Built-in schemes (`pathlib_next.uri.schemes`)

Every class is dispatched by `UriPath(...)`; `pathlib_next.uri.schemes`
re-exports `FileUri`, `DataUri`, `HttpPath`, `DavPath`, `FtpPath`, `SftpPath`,
`S3Path`, `GsPath`, `AzPath`, `GitHubPath`, `GitLabPath`, `GitPath`, `ZipUri`,
`TarUri` lazily (every name imports without its extra; see "Package root"). Backends are passed
as `UriPath(uri, backend=...)` or `path.with_backend(...)`.

### `FileUri` — `file:` (`pathlib_next.uri.schemes.file`)

```python
class FileUri(UriPath):
    filepath
```

`file:///abs`, `file:rel`,
`file://localhost/C:/x`. `filepath -> LocalPath`; all I/O delegates to it
(listing reuses `LocalPath`'s scandir). `rename()` accepts local targets
only and returns a `FileUri`. No `symlink_to()`/`readlink()`. On Windows a
drive is the anchor: `parents` ends at the drive root (`file:/C:/`) and
equals the repeated `parent`, dot segments never climb above it, and a
`str` join key or `copy()`/`move()`/`rename()` destination reads `\` as a
separator (`C:\Temp\x` is an absolute path). A path with a named host is a
UNC path there, whose anchor is the share: `file://server/share/a/b` has the
ancestors `file://server/share/a` and `file://server/share/`, and the share
root is its own parent. A `FileUri` argument holding a drive
(`base / UriPath("file:///D:/f")`) restarts the join, as a `str` key does.
Elsewhere a backslash is a filename character and the root of a host is the
end of the chain, and every other scheme splits on `/` only.

### `DataUri` — `data:` (`pathlib_next.uri.schemes.data`)

```python
class DataUri(UriPath):
    mediatype: str
```

RFC 2397
`data:[<mediatype>][;base64],<data>`. `mediatype` property (default
`text/plain;charset=US-ASCII`). RFC 2397 has no query: an unescaped `?` and
what follows it are payload (`data:,a?b` reads `a?b`), while a `#` starts the
fragment. Read-only single file: `open("r")` only,
`stat().st_size` is the decoded size, listing → `NotADirectoryError`.

### `HttpPath` — `http:`, `https:` (`pathlib_next.uri.schemes.http`, `http` extra)

```python
class HttpPath(UriPath):
    def with_session(
        self, session, write_method="PUT", append_mode="rewrite",
        **requests_args,
    ): ...
    def stat(self, *, follow_symlinks=True, walk_up_last_modified=False): ...

class HttpBackend(NamedTuple):
    def __init__(
        self, session, requests_args, write_method="PUT",
        append_mode="rewrite",
    ): ...
    def request(self, method, uri, **kwargs): ...

DEFAULT_TIMEOUT = (10, 60)
MAX_LISTING_BYTES = 8388608
```

- `with_session()` — installs an `HttpBackend` (a `NamedTuple` with `request()`); `requests_args` (`headers=`, `auth=`,
  `verify=`, `timeout=`, ...) go to every request, request-specific
  headers merge over them.
- Timeout: `DEFAULT_TIMEOUT = (10, 60)` (connect, read) unless given;
  `timeout=None` waits forever.
- Redirects: `GET`/`HEAD`/`OPTIONS` follow them as `requests` does. Every
  other request (`PROPFIND`, `write_method`, `PATCH`, `DELETE`, `MKCOL`,
  `MOVE`) is sent with `allow_redirects=False`, and an `allow_redirects` in
  `requests_args` or the call does not change that: a 307/308 (for
  `PROPFIND` also a 301, 302 or 303) to the same scheme, host and port is
  re-sent once with the same method, headers and body; any other 3xx,
  another origin or a second redirect raises `OSError(EIO)` naming the
  status and the `Location` (userinfo removed). A body that is not bytes or
  `str` cannot be re-sent, so it raises too. A followed `GET`/`HEAD` redirect
  to another host drops `Authorization` only (`requests`' rule): headers
  other than `Authorization` from `with_session(headers=...)` (`X-Api-Key`,
  a vendor token header) are sent to the new host as well, so keep a secret
  in `Authorization` or `auth=`, or point the path at the final URL.
- URL userinfo is sent as Basic `auth=` (not in the URL) unless
  `requests_args`/`session.auth` set auth; it takes priority over `~/.netrc`.
  Percent-escapes stand for octets, and those octets are the credential:
  `caf%C3%A9` is sent as UTF-8 bytes, `%FF` as the byte `FF`.
- `stat()` — `HEAD`
  (`GET` on 405); a redirect is followed once, at its `Location`; a final
  URL whose path ends in `/` is a directory (a query or fragment does not
  matter); `st_size` from `Content-Length` (absent, non-numeric or negative
  = unknown, `0`), `st_mtime` from `Last-Modified` (UTC), or from the
  parent's index when `walk_up_last_modified=True`.
- Listing scrapes an Apache/nginx-style HTML index, asking for the
  directory's URL with a trailing `/` first; `.`/`..` rows are never
  children; a non-HTML response → `NotADirectoryError` (an HTML file lists
  as empty). Cannot always tell a file from an index page. The body is read
  like `open()` reads a file: a cut-short, stalled or undecodable one is
  `OSError(EIO)`/`TimeoutError` naming the path, never a partial list, and
  one over `schemes.http.MAX_LISTING_BYTES` (8 MiB after decoding; assign a
  larger number to allow more) is `OSError(EFBIG)`. A name that is not
  UTF-8 (`caf%E9`) keeps its bytes (`surrogateescape`) and is requested as
  listed.
- `open("r")` streams `GET` with `Accept-Encoding: identity`; the mode is
  matched exactly, so `"r+"`, `"w+"` and `"a+"` raise `NotImplementedError`.
  `"w"`/`"x"`
  buffer and send `write_method` on close. `"x"` first calls `stat()`: found
  → `FileExistsError`, a failure other than not-found raises with nothing
  sent; the upload then carries `If-None-Match: *`, and a 412 reply →
  `FileExistsError` (a server that ignores the header leaves the window
  between probe and upload open). `"a"`: `append_mode="rewrite"` (GET + full
  re-upload, not atomic) or `"patch"` (`PATCH` with `Content-Range` from the
  `HEAD` size; a `HEAD` reply without `Content-Length` → `OSError(EIO)`
  naming the path, nothing sent; a refusal raises `PermissionError` for
  401/403/405/501, `OSError(EIO)` for other statuses). An `open("a")` that
  raises (the read of the current content, or the `HEAD` of patch mode,
  failed) has uploaded nothing.
- `unlink()` sends `DELETE` and refuses a directory (`IsADirectoryError`,
  judged by `stat()`); `rmdir()` requires an empty directory. No `mkdir()`,
  `rename()`, `chmod()`.
- Status mapping: 404/410 → `FileNotFoundError`, 401/403/405/501 →
  `PermissionError`, 409 → `FileExistsError` (`FileNotFoundError` for
  writes), other → `OSError(EIO)` with the status.

### `DavPath` — `dav:`, `davs:` (`pathlib_next.uri.schemes.dav`, `http` extra)

```python
class DavPath(HttpPath):
    def stat(self, *, follow_symlinks=True, walk_up_last_modified=False): ...
```

Same backend and `with_session()`. `stat()`/
listing via `PROPFIND` (`stat()` takes `walk_up_last_modified=` and ignores
it: the reply carries the modification time). A reply that is not a
`multistatus` document, or that names neither the collection nor any
member of it, is `OSError(EIO)`; a listing keeps a member only when its
href, resolved against the collection, names the same host and a direct
child, and a `getcontentlength` that is not a plain number is unknown (`0`).
`open("r")` on a collection → `IsADirectoryError`;
`"w"`/`"x"` send `write_method` (default `PUT`) on close (`"x"` as for
`HttpPath`, probing with `PROPFIND`); `"a"` unsupported, so `append_mode`
has no effect. `mkdir()` = `MKCOL`
(missing parent → `FileNotFoundError`). `unlink()` refuses a collection;
`rmdir()` checks emptiness first; `rm(recursive=True)` is one recursive
`DELETE` (failed members of a 207 raise). `rename()` = `MOVE` with
`Overwrite: F` (existing target → `FileExistsError`), no credentials in
`Destination`, which carries the target's own query (never the source's)
and no fragment. 423 → `PermissionError`. `PROPFIND`, `PUT`, `MKCOL`, `DELETE`
and `MOVE` follow the `HttpPath` redirect rule above (a listing is scoped to
the URL that answered it). No `chmod()`.

### `FtpPath` — `ftp:`, `ftps:` (`pathlib_next.uri.schemes.ftp`)

```python
DEFAULT_TIMEOUT = 30.0
IDLE_PROBE_SECONDS = 1.0

class FtpBackend(BaseFtpBackend):
    def __init__(
        self, timeout=30.0, ssl_context=None, verify=True,
    ) -> None: ...

class BaseFtpBackend:
    def client(self, source, tls) -> _ftplib.FTP: ...
```

- `FtpBackend` — `timeout`
  bounds connect, replies and transfers (`None` = forever). `ftps:` is
  explicit TLS with `PROT P`; the certificate and host name are verified
  with `ssl.create_default_context()`; `ssl_context` (e.g.
  `ssl.create_default_context(cafile=...)`) wins over `verify`;
  `verify=False` accepts any certificate. Data connections reuse the TLS
  session. No user in the URI → anonymous login.
- `BaseFtpBackend.client()` — override to supply
  connections.
- Paths without `backend=` share one default `FtpBackend()`. Connections
  are cached per (backend, source, tls, thread) (LRU of 128, closed on
  eviction, and closed when their thread ends). A connection that carried
  a command within the last `schemes.ftp.IDLE_PROBE_SECONDS` (1 second) is
  used as it is; one idle for longer, or not yet used, is probed with `NOOP`
  and replaced when dead.
- A listing uses `MLSD` (UTC `modify`; mode from `unix.mode` or the `perm`
  fact); `stat()` uses `MLST` of the entry itself where the server's `FEAT`
  lists it (one reply on the control connection, `FEAT` asked once per
  connection), else the parent's `MLSD`; a server without `MLSD` falls back
  to `NLST` for a listing and `SIZE`/`CWD` for a stat. A `size` fact that is
  not a plain non-negative number is unknown (`st_size` 0). A listing is
  read whole inside the guarded call: a transient `4xx` reply is
  `OSError(EAGAIN)`, a name the client cannot decode is `OSError(EILSEQ)`
  for that directory only, and a listing that dies mid-transfer drops the
  connection, so the next request reconnects. `stat()` of another entry in
  a directory that holds an undecodable name is answered by `MLST`, or on a
  server with `MLSD` and no `MLST` by `SIZE`/`CWD` (after a reconnect).
- An empty path (`ftp://host`) is the root, as `ftp://host/` is: `/` is
  sent in every command that takes a path, never an empty argument (which a
  server reads as its working directory).
- Reads download the whole file into memory; `"w"`/`"x"`/`"a"` (`APPE`)/
  `"r+"` buffer in memory and upload on close (`"x"` checks then writes).
- `rename()` on the same server. Whether an existing target is replaced
  is the server's: a POSIX one replaces it, a Windows one answers `550 File
  exists`, which is `FileExistsError` when the target is there (a reply that
  names a permission problem stays `PermissionError`). `chmod()` via `SITE
  CHMOD` (`NotImplementedError`, chained to the server's reply, when the
  server lacks it or refuses it, or `follow_symlinks=False`).

### `SftpPath` — `sftp:` (`pathlib_next.uri.schemes.sftp`, `sftp` or `sftp-async` extra)

```python
class SftpPath(UriPath):
    def __init__(self, *args, ssh_config=..., **kwargs): ...
    def readlink(self) -> SftpPath: ...
    def hardlink_to(self, target): ...
    def chmod(self, mode, *, follow_symlinks=True): ...
    def chown(self, uid=None, gid=None, *, follow_symlinks=True) -> None: ...
    def checksum(self, algorithm="md5") -> str: ...
    def supported_checksums(self) -> FrozenSet[str]: ...
    def host_fspath(self) -> str: ...

class BaseSftpBackend:
    def client(self, source): ...
    def close(self) -> None: ...
    # capability switches
    supports_tree = False
    supports_lchmod = False
    supports_hardlink = False
    def tree_copy(
        self, path, target, *, overwrite, follow_symlinks, preserve_metadata,
        ignore_error,
    ) -> None: ...
    def tree_rm(self, path, *, missing_ok, on_error) -> None: ...
    def checksum(self, path, algorithm) -> str: ...
    def supported_checksums(self, path) -> FrozenSet[str]: ...
```

- `SftpPath(*uris, backend=None, ssh_config=<default>)` — `ssh_config`:
  default `~/.ssh/config`, `None` for none, a path or iterable of paths;
  carried to every path derived from it on the same endpoint (`/`, `parent`,
  `with_name()`, `with_source()`, a join, a destination string), and dropped,
  with the backend, when the derived path is on another host. Paths of one
  endpoint share the backend they build for themselves unless their
  `ssh_config` differs: one built with another `ssh_config` than the path it
  derives from has a backend of its own. A host that is not a plain host name or
  address -- an IP address (an IPv6 one may carry a `%zone`), or ASCII
  letters, digits, `.`, `_`, `-` (not first) and the letters and digits of
  other scripts -- raises `ValueError` naming it where a connection or an
  ssh_config lookup would start, on both backends; building, joining and
  comparing such a path still work.
- Backend selection, highest first: `backend=` → subclass attribute
  `_default_backend_cls` → env `PATHLIB_NEXT_SFTP_BACKEND`
  (`auto`|`asyncssh`|`paramiko`; a named backend that is not installed
  raises `ImportError`) → auto (asyncssh if importable, else paramiko).
- Errors are the same on both backends and none is chained to the SSH
  library's exception. A server status is the pathlib exception with `errno`
  and `filename` (the remote path; `filename2` for the second path of
  `rename()` and `symlink_to()`); an "operation unsupported" status is
  `NotImplementedError`. An error `SftpPath` builds itself (`mkdir()` of an
  existing path, `rmdir()` or `iterdir()` of the wrong kind of entry) carries
  the URI as `filename`. A connection that goes away, mid-request or
  mid-transfer, is `ConnectionResetError`; a refused login is
  `SftpAuthenticationError` (a `PermissionError`, `EACCES`); a host key that
  is unknown, changed or refused is `SftpHostKeyError` (a `ConnectionError`,
  `ECONNABORTED`) and no credential was sent; any other failure of the
  handshake or the session is `ConnectionAbortedError`; a refused TCP
  connection is `ConnectionRefusedError`; a timeout is `TimeoutError` (a
  banner that never arrives is reported as `ConnectionAbortedError` by
  paramiko). Both exception classes import from `pathlib_next.uri.schemes.sftp`.
  The SSH library's own message for a `ConnectionAbortedError` (and for the
  rare request failure reported as `OSError(EIO)`) is logged at DEBUG on the
  logger `pathlib_next.sftp`, as `SFTP connection failed: <ExceptionClass>:
  <message>`; the exception carries only the class name. A DEBUG record can
  hold what the SSH library put in its message, so enable that logger for
  troubleshooting and keep it out of shared logs.
- A name the server sends that is not UTF-8 is the string of its bytes as lone
  surrogates (pathlib's spelling on POSIX), on both backends: it lists beside
  its siblings, and `stat()`, `open()`, `unlink()`, `rm()` and `copy()` send
  the same bytes back.
- Both backends: `close()` closes every cached connection (the backend stays
  usable); `default(ssh_config=...)` classmethod.
  `BaseSftpBackend.client(source)` is the override point
  (`supports_lchmod`, `supports_hardlink`, `checksum()`,
  `supported_checksums()`). `supports_tree` (False; asyncssh True) says the
  backend has its own concurrent recursive `copy()` and `rm()`
  (`tree_copy(path, target, *, overwrite, follow_symlinks, preserve_metadata,
  ignore_error)`, `tree_rm(path, *, missing_ok, on_error)`), which `SftpPath`
  then uses for a directory on one host; otherwise (and for every backend
  that does not set it) the generic walks run, and no SSH library is
  imported by `copy()` or `rm()`. Listing a directory and testing each child
  (`is_dir()`, `is_file()`, `exists()`) asks the server once: a listed
  entry that is not a link is not stat-ed again, and the native `rm()` looks
  only at the root before it lists.
- `readlink() -> SftpPath` (verbatim target) and `symlink_to()` on both
  backends; `hardlink_to(target)` and `chmod(follow_symlinks=False)` on
  asyncssh only (paramiko → `NotImplementedError`); `chown()` with numeric
  ids only. A partial `chown()` (`chown(None, gid)`) needs the current id of
  the field it leaves alone: a server that names owners instead of numbering
  them (SFTP version 4) makes it `NotImplementedError` naming the field, and
  nothing is sent (`st_uid`/`st_gid` are 0 for an owner name that is not a
  number).
  `rename()` replaces an existing target via `posix-rename@openssh.com`
  where supported, else `FileExistsError`. `checksum()`/
  `supported_checksums()` (`NativeChecksum`): both backends send the
  `check-file-handle` extension (OpenSSH lacks it); an "operation
  unsupported" answer is remembered per connection and algorithm (one request
  per algorithm, not per file) and a failure for one file refuses nothing;
  an algorithm other than `md5`, `sha1`, `sha224`, `sha256`, `sha384`,
  `sha512` or `crc32`, or a digest of the wrong size, is
  `NotImplementedError` (no request for the first); every failure is
  `NotImplementedError`. `supported_checksums()` is empty until a digest has
  been produced. `__fspath__()`/`host_fspath()` return `.path`.
- A file opened for writing is flushed and closed when the interpreter exits
  if the program left it open (a text wrapper's pending text included),
  waiting at most five seconds; `write()` returns the number of bytes
  written and `truncate()` works on both backends.

#### `SftpBackend` (paramiko)

```python
class SftpBackend(BaseSftpBackend):
    def __init__(
        self, connect_opts=None, hostkeypolicy=None, ssh_config=..., *,
        known_hosts=..., timeout=30.0,
    ) -> None: ...
    @classmethod
    def default(cls, ssh_config=...) -> SftpBackend: ...
    def close(self) -> None: ...
```

Host keys are verified: `known_hosts` default is
`~/.ssh/known_hosts` plus ssh_config `UserKnownHostsFile` (`None` loads
none; a path or list loads exactly those), `hostkeypolicy` default
`paramiko.RejectPolicy()`; a changed key always fails. Opt-out:
`SftpBackend(opts, paramiko.AutoAddPolicy(), known_hosts=None)`.
`timeout` fills paramiko's connect/banner/auth/channel timeouts
(`connect_opts` values win; `None` leaves them unset); requests on an open
connection are unbounded. ssh_config: `HostName`, `Port`, `User`,
`IdentityFile`, `ProxyCommand`, `Include`; `ProxyJump` →
`NotImplementedError` unless `connect_opts["sock"]` is given. A
`ProxyCommand` expands `%h`, `%n`, `%p`, `%r` and `%%` with the host,
port and user this connection uses (a user with white space, a quote, a
shell metacharacter or a leading `-` is refused when `%r` is used, on
asyncssh too: a `ValueError` before asyncssh is given the user, when the
config's `ProxyCommand` would pass it on); an
`Include` inside a `Host` or `Match` block applies the included file's
blocks only where the enclosing one does. Connections cached per
(backend, source, thread), replaced when dropped.

#### `AsyncsshSftpBackend`

```python
class AsyncsshSftpBackend(BaseSftpBackend):
    def __init__(
        self, connect_opts=None, *, max_concurrency=None, sftp_version=4,
        ssh_config=..., timeout=60.0,
    ): ...
    @classmethod
    def default(cls, ssh_config=...) -> AsyncsshSftpBackend: ...
    def close(self) -> None: ...
    DEFAULT_MAX_CONCURRENCY = 16
```

. asyncssh verifies
host keys against `known_hosts`/ssh_config; opt-out
`connect_opts={"known_hosts": None}`. The port is the URI's, else the
ssh_config `Port` of the host. `timeout` bounds single requests
(a timed-out request is cancelled and raises `TimeoutError`); recursive
`copy()`/`rm()`, streamed reads/writes and a native `checksum()` are
unbounded (use asyncssh's
`connect_timeout`/`keepalive_interval`). One connection per (backend,
source), served by one shared background event loop thread; not
fork-safe (rebuilt after `fork()`). A sync `Path` call made on that loop
thread (inside a callback running there) raises `RuntimeError`.
`max_concurrency` (`None` → `DEFAULT_MAX_CONCURRENCY = 16`) bounds
requests in flight and files open during recursive `copy()` (target on
the same host and the same tree: two distinct supplied backends copy
through the generic walk) and `rm()`. Such a call that raises, times out
or is interrupted (`KeyboardInterrupt`) has stopped when the exception
reaches the caller: no further request is sent, and a destination file
whose copy did not complete is removed. Both walks leave behind what the
generic `copy()`/`rm()` leave: a source is opened before the destination
it replaces is touched, `copy()` applies permission bits as
`Path.copy()` does, `rm()`'s `missing_ok` covers the path it was called on
only, and `ignore_error` is offered each error once.

### `S3Path` — `s3://bucket/key` (`pathlib_next.uri.schemes.s3`, `s3` extra)

```python
class S3Path(UriPath):
    bucket: str
    key: str

class S3Backend(BaseS3Backend):
    def __init__(self, **client_kwargs): ...

class BaseS3Backend:
    def client(self): ...
```

`bucket`,
`key` (one trailing `/` dropped: `s3://b/dir/` is `dir`).
`S3Backend(**client_kwargs)` → one lazily built, thread-shared
`boto3.client("s3", **client_kwargs)` (default: boto3's own credential and
endpoint configuration; threads racing for the first request build one);
`BaseS3Backend.client()` is the override point. `S3Backend`, `GsBackend`
and `AzBackend` pickle and copy (`copy.copy`, `deepcopy`) without the client
they built and keep their options (`client_kwargs`, credentials included):
the copy builds its own client on first use. A pickled path never carries
them: it holds the URI, and the receiving process uses the default backend
unless the program supplies one (a backend class that sets `picklable =
True` is pickled with its path, secrets and all).

- Directories are key prefixes: `mkdir()` writes a zero-byte `key/` marker,
  `rmdir()` needs an empty prefix, a key that is both an object and a prefix
  is the object (in `stat()` and listings). No hierarchy enforcement (writes
  below a missing "directory" succeed), but a write (`"w"`) or `rename()`
  onto a prefix directory raises `IsADirectoryError` (one list request;
  credentials that may write but not list are not stopped). `mkdir()` treats
  only `FileNotFoundError` from `stat()` as absent; any other probe error is
  raised and no marker is written.
- Reads stream; `"w"`/`"x"`/`"r+"` spool and upload on close; `"x"` is a
  conditional put (check-then-put above 5 GiB); `"a"` unsupported.
  `st_mtime` from `LastModified`.
- Errors (the same on `GsPath` and `AzPath`): the store's answer is
  `FileNotFoundError` (a key or a bucket), `PermissionError`, or
  `OSError(EIO)`; a request that runs out of time is `TimeoutError`, an
  endpoint that cannot be reached `ConnectionError`, a connection that
  breaks while the answer or a body arrives `ConnectionResetError`. Each
  names the path as `filename`, none is chained to the SDK's exception, and
  the message carries the SDK's exception type but not its text (it holds
  the request URL). They reach `exists()` and `walk(on_error=)` as
  `OSError`. A bucket root read or written is `IsADirectoryError`; a path
  with no bucket does not exist (`FileNotFoundError`, nothing is sent).
  `rm(recursive=True)` of a missing bucket is a missing path
  (`missing_ok`, `ignore_error` decide); a key a batch delete refuses is
  offered to `ignore_error(error, path)` with that key's path, one error
  per key. `rename()` of a missing path onto its own name is
  `FileNotFoundError`.
- `rename()`: server-side copy + delete in the same bucket; a prefix
  directory → `NotImplementedError` (`move()` copies). `rm(recursive=True)`
  batch-deletes; at the bucket root → `PermissionError`. No `chmod()`.
- A prefix that holds keys a directory walk does not reach (a key with an
  empty, `.` or `..` segment; the subtree under a key that is also an
  object; a `name/` key that holds data) is refused by
  `copy(recursive=True)`, `move()` and `rm(recursive=True)`:
  `OSError(EINVAL)` naming the keys, nothing created or removed
  (`rm(..., ignore_error=...)` is offered the error and then removes only the
  keys a walk reaches). Remove the named keys, or `unlink()` the object
  that hides a subtree, first. `iterdir()`/`walk()` skip such keys as
  before.

### `GsPath` — `gs://bucket/key` (`pathlib_next.uri.schemes.gs`, `gs` extra)

```python
class GsPath(UriPath):
    bucket_name: str
    key: str

class GsBackend(BaseGsBackend):
    def __init__(self, *, timeout=..., retry=..., **client_kwargs): ...

class BaseGsBackend:
    def client(self): ...
    def call_options(self) -> dict: ...
```

`bucket_name`,
`key`. `GsBackend(**client_kwargs)` → `google.cloud.storage.Client(
**client_kwargs)` unchanged (emulator: `client_options={"api_endpoint":
url}, use_auth_w_custom_endpoint=False`, or set `STORAGE_EMULATOR_HOST`
yourself); `BaseGsBackend.client()`. `GsBackend(*, timeout=, retry=,
**client_kwargs)`: `timeout` (seconds or `(connect, read)`) and `retry`
(`google.api_core.retry.Retry`, or `None` for none) are passed to every SDK
call a path makes (`BaseGsBackend.call_options()` is the hook); left out,
the SDK's defaults apply, under which an unreachable endpoint fails after
about two minutes. Same prefix model, rename, error and
recursive-copy/remove rules as `S3Path` (same bucket); `rmdir()` of the
bucket root → `PermissionError`; reads load the whole object; `"x"` uses
`if_generation_match=0`; `"a"` unsupported; `st_mtime` from `updated`.

### `AzPath` — `az://account/container/key` (`pathlib_next.uri.schemes.az`, `az` extra)

```python
class AzPath(UriPath):
    account: str
    container: str
    key: str

class AzBackend(BaseAzBackend):
    def __init__(self, account=None, **client_kwargs): ...

class BaseAzBackend:
    def client(self): ...

COPY_POLL_TIMEOUT = 300.0
```

`account`, `container`, `key` (one trailing `/` dropped, interior `//`
kept). `AzBackend(account=None, **client_kwargs)`: `connection_string=` →
`BlobServiceClient.from_connection_string(...)`; otherwise kwargs go to
`BlobServiceClient` and `account` alone derives
`account_url="https://<account>.blob.core.windows.net"` with
`azure-identity`'s `DefaultAzureCredential` unless `credential=` is passed.
Without `backend=`, one shared backend per URI account is used, which needs
`azure-identity` (installed by the `az` extra; `ImportError` otherwise). Same
prefix model, error and recursive-copy/remove rules as `S3Path` (a name
that is both a blob and a prefix is the blob, whatever order the SDK lists
them in; timeouts and retries are the SDK's own `retry_*`, `connection_timeout=`
and `read_timeout=` client options); `az://account` is a directory whose
children are the containers; a path with no account and no `backend=`
does not exist; `rmdir()` of the container root → `PermissionError`;
`rename()` within one container waits for the server-side copy for at most
`schemes.az.COPY_POLL_TIMEOUT` (300 s), then aborts it and raises
`TimeoutError` with the source untouched; `"x"` sends
`If-None-Match: *`; `"a"` unsupported; `st_mtime` from `last_modified`.

### `GitHubPath` — `github://[TOKEN@]host/owner/repo/path?ref=REF` (`pathlib_next.uri.schemes.github`, `pathlib_next.uri.schemes._gitrepo`, `http` extra)

```python
class GitHubPath(UriPath):
    owner: str
    repo: str
    repo_path: str
    ref: str | None

class RepoBackend(BaseRepoBackend):
    def __init__(
        self, token=None, session=None, api_base=None, **requests_args,
    ): ...

class BaseRepoBackend:
    def request(self, method, url, **kwargs): ...
```

Read-only; `open()` other than `"r"` and every
write method raise `NotImplementedError`.

- Properties: `owner`, `repo`, `repo_path`, `ref` (`None` = default
  branch); `?ref=` is carried to every child.
- `RepoBackend` (`schemes.github`; `BaseRepoBackend` is in `schemes._gitrepo`): `Authorization: Bearer <token>`, timeout default
  `(10, 60)`, `cache` dict; `api_base` overrides the API root;
  `BaseRepoBackend.request(method, url, **kwargs)` is the override point.
  A request that carries `Authorization` (the token, or one given in
  `headers=`) to a plain `http://` URL on a host other than loopback is
  still sent, with an `InsecureTransportWarning` (a `UserWarning`,
  importable from `schemes._gitrepo`, `schemes.github` and
  `schemes.gitlab`).
  Without `backend=`, the token is the userinfo password
  (`x-access-token:TOKEN@`) or else the bare user (`TOKEN@`).
- `str()`, `repr()`, `as_posix()` and `as_uri(sanitize=True)` drop the whole
  userinfo. Owner, repository and every path segment are percent-encoded
  into the API URL, so none can add a query or a segment, and a `.` or `..`
  segment in the path or the ref (made with `with_name("..")` or
  `with_path()`; a URI's own dot segments are already resolved) raises
  `ValueError` instead of leaving the repository's API root.
- API root `https://api.github.com` for `github.com`, else
  `https://host[:port]/api/v3`. Contents API listings (a directory at the
  1,000-entry cap is re-read through the Git Trees API); file bodies use
  the raw media type; symlink/submodule entries read as files;
  `st_mtime` is `0`.
- Rate limits (403/429 with limit headers, any 429) → `OSError(EAGAIN)`.
- A JSON reply that is not the documented shape (HTML from a proxy, `null`,
  a truncated body, an array where an object is expected, an entry without
  a name and a type, a size that is not a non-negative integer) is
  `OSError(EIO)` naming the path, so `exists()` is `False` and `stat()`
  raises. A raw file body has no shape to check and is returned as sent.

### `GitLabPath` — `gitlab://[TOKEN@]host[:port]/owner/repo/path` (`pathlib_next.uri.schemes.gitlab`, `http` extra)

```python
class GitLabPath(UriPath):
    owner: str
    repo: str
    repo_path: str
    ref: str | None
```

Same backend, properties and read-only
contract; API root `https://host[:port]/api/v4` (`gitlab.com` by
default). Without `?ref=` the default branch is fetched once
(`GET /projects/:id`) and cached in `backend.cache`. Tree listings are
paginated (100 per page); file entries carry no stat hint (a `stat()` per
file, which is a `HEAD` of the files endpoint reading `X-Gitlab-Size`; a
reply without it, or a refused `HEAD`, is followed by a `GET` of the file's
metadata); `st_mtime` is `0`. Replies of the wrong shape are
`OSError(EIO)` as for `GitHubPath`.

### `GitPath` — `git:` (`pathlib_next.uri.schemes.git`)

```python
class GitPath(UriPath): ...
class GitHubGitPath(GitHubPath): ...
class GitLabGitPath(GitLabPath): ...
```

`git://github.com/...` constructs a
`GitHubPath`, `git://gitlab.com/...` a `GitLabPath`; any other host →
`ValueError`. `git+github:` (`GitHubGitPath`) and `git+gitlab:`
(`GitLabGitPath`) pin the provider for any host.

### Archives — `zip:`, `tar:`, `archive:` (`pathlib_next.uri.schemes.archive`, `pathlib_next.uri.schemes.archive._base`)

```python
MEMBER_SPOOL_BYTES = 16777216

class ArchiveUri(UriPath):
    def refresh(self) -> None: ...
class ZipUri(ArchiveUri): ...
class TarUri(ArchiveUri): ...
ArchiveZipUri = ZipUri
ArchiveTarUri = TarUri
```

`ZipUri` (`zip:`), `TarUri` (`tar:`),
`ArchiveUri` (`archive:`, detects the format from the outer name
`.zip`/`.jar` vs `.tar`/`.tgz`/`.tar.*`, else a `PK` magic sniff),
`ArchiveZipUri` (`archive+zip:`), `ArchiveTarUri` (`archive+tar:`). The last
two are second scheme names of `ZipUri` and `TarUri`, and the class names
are aliases of those (`ArchiveZipUri is ZipUri`); a path prints and pickles
with the scheme it was written with.

- `archive:` settles the format on first use, not when the path is built:
  constructing, printing, joining, copying and pickling a path read nothing,
  a non-local outer that had to be read to decide is not fetched a second
  time, and a failure to read the outer propagates and decides nothing, so
  the next call tries again (an `archive:` path to an outer that is missing
  and has no extension raises `FileNotFoundError` on a write, as it does on
  a read).
- Syntax `<scheme>:<archive-uri>!/<member>`; `<archive-uri>` must carry a
  scheme (`ValueError` otherwise) and may be any URI, including another
  archive (each leading archive scheme consumes one `!/`; nested archives
  are read-only). A member name containing `!/` is written `%21/`.
  `name`/`parent`/`glob()` work on the member path; `as_uri()`
  percent-encodes it, and a name that is not valid UTF-8 (a tar member
  written in another charset, held as surrogate escapes) keeps its bytes as
  `%XX` (`caf%E9.txt`), so such a path prints, hashes, compares and parses
  back to the same member.
- One shared handle per archive (keyed by the real local path, or the outer
  URI), released when no path references it. A local zip or tar is read from
  its file as needed (a compressed tar is decompressed from its start for
  each member it reaches) and no OS handle is held between calls; a
  non-local outer is read whole into memory, once, and kept while any path
  to it lives. `ArchiveUri.refresh()` forgets what is held of the outer, so
  the next use reads it again (every path to the same archive shares it);
  it is how a change at a remote source is seen, and a local archive needs
  none (it is checked against the file on every use).
- Cost. The members, the directories and each entry's stat data are derived
  once per open handle, so a listing, a `walk()` or a `stat()` costs one
  validation of the file and work in the size of the directory, not of the
  archive. A member read is copied out of the shared handle: in memory up to
  `schemes.archive._base.MEMBER_SPOOL_BYTES` (16 MiB) and past that into an
  unnamed temporary file, so memory stays small however large the member is,
  and the disk used is the member's size as the archive declares it (an
  archive can declare more than it holds). `open("r+")` and a write buffer
  hold the whole member in memory. A zip mutation copies the entries it does
  not change as they lie in the archive, without decompressing them, so it
  costs the archive's stored size; an encrypted member, or one stored with a
  compression method `zipfile` lacks, does not stop a change to another
  member.
- **Member names are normalized POSIX relative paths**, whatever the
  writer emitted and whichever format: a leading `./` (`tar -C dir .`,
  `shutil.make_archive`), empty segments (`a//b`) and interior `.`/`..`
  (`a/./b`, `a/b/../c`) resolve, so one member has one name and a listing
  and a lookup always agree. The spelling as written still addresses the
  member. A name that would leave the root -- `../x`, `/abs`, or a `..`
  with nothing to spend it on -- has no name inside the archive: it is
  never listed, never readable, and cannot be written (the write fails and
  creates nothing). A name only a *Windows destination* would misread
  (`C:drive.txt`, `a\b`) IS a member, because it is an ordinary POSIX
  filename; refusing to join it is the destination's rule, applied by
  whatever writes there (see `copy()` below, `PathSyncer`,
  `unpack_archive()`). `zipfile` itself rewrites `\` to `/`, so a
  backslash name only survives in a tar. When two spellings normalize to one name the
  later member wins, as in `zipfile`/`tarfile`. Exception types are POSIX on every
  platform.
- The archive root is the archive: `exists()`/`is_dir()`/`stat()` of it
  open the archive, so a missing one does not exist (`FileNotFoundError`
  from `stat()`) and a file that is not an archive raises
  `zipfile.BadZipFile` / `tarfile.ReadError`, as a listing does. So does any
  other damage that stops the archive from being read -- a bad deflate
  stream, a short or inconsistent header, a zip version `zipfile` does not
  know, a truncated or corrupt gzip/bzip2/xz stream -- whichever decoder
  noticed it, and never as `zlib.error`, `struct.error`, `EOFError` or an
  `OSError` without an errno. A member that is encrypted raises
  `RuntimeError`, and one stored with a compression method `zipfile` lacks
  `NotImplementedError`, as `zipfile` raises them. A member another thread's
  rewrite removed between the lookup and its stat is `FileNotFoundError`.
- A file has no children: with a file `y` and a member `y/z`, `y/z` does
  not exist for `exists()`, `stat()`, `open()`, `unlink()` and `rename()`
  (`FileNotFoundError`; writing or `mkdir()` below `y` is
  `NotADirectoryError`), and no listing shows it.
- Writes: zip only, and only with a local `file:` outer (else
  `NotImplementedError`). `"w"`/`"x"`/`"r+"`, `mkdir()`, `unlink()`,
  `rmdir()`, `rename()` (same archive; replaces like POSIX `rename`;
  a destination below a file or a missing directory is
  `NotADirectoryError` / `FileNotFoundError`, a directory into itself
  `OSError(EINVAL)`); parents must exist; `"a"` unsupported. Every mutation
  replaces the archive atomically (temp file + `os.replace`) and keeps
  other members' metadata, the comment and any prefix bytes. A write uses
  the normalized name; a name that escapes the root fails and creates
  nothing. A new member is stored deflated; an existing member that is
  overwritten keeps its compression method. The archive is replaced by a
  new file, so another hard link to it keeps the old content and the
  writing user owns the result; an archive the caller may not write
  (`os.access(..., W_OK)`) is refused with `PermissionError`, on every
  platform, before anything is written.
- Removing a file never removes its directory: when `unlink()`, `rename()`
  or `rm()` takes the last member out of a directory that exists only
  through its members, the directory's own entry (`name/`) is written in
  the same rewrite. `rmdir()` of the root succeeds on an empty archive
  (`OSError(ENOTEMPTY)` otherwise) and removes nothing.
  `rm(recursive=True)` of a directory is ONE rewrite, and raises
  `OSError(ENOTEMPTY)` removing nothing when the tree holds a member under
  a file.
- A member the archive spells several ways (`norm` and `./norm`) is one
  member: `unlink()`, `rename()`, `rmdir()` and `rm()` act on every entry
  that names it, and renaming over it replaces them all.
- `tar:` (plain, gz, bz2, xz) is read-only.

## Exceptions

A failure is the exception `pathlib` raises for the same condition
(`FileNotFoundError`, `FileExistsError`, `IsADirectoryError`,
`NotADirectoryError`, `PermissionError`, or an `OSError` with an `errno`),
`NotImplementedError` for an operation a class does not support, and
`ValueError`/`TypeError` for a bad argument. Those the package defines are
listed here; the conditions that raise them are in the entries.

### Core (`pathlib_next.utils.glob`)

```python
class NonRelativePatternError(NotImplementedError, ValueError): ...
```

`glob()` raises it for an absolute pattern.

### URI layer (`pathlib_next.uri.schemes.sftp`, `pathlib_next.uri.schemes.github`)

```python
class SftpAuthenticationError(PermissionError): ...  # errno.EACCES
class SftpHostKeyError(ConnectionError): ...  # errno.ECONNABORTED
class InsecureTransportWarning(UserWarning): ...  # also in schemes.gitlab
```

Unsupported
operations raise `NotImplementedError`. Network errors map to pathlib types
(`FileNotFoundError`, `PermissionError`, `FileExistsError`,
`IsADirectoryError`, `NotADirectoryError`, `OSError(ENOTEMPTY)`), timeouts to
`TimeoutError`, anything else to `OSError`; transport exceptions are not
chained (their text can carry credentials).

`SftpAuthenticationError` is a refused login and `SftpHostKeyError` a host
key that is unknown, changed or refused (see `SftpPath`);
`InsecureTransportWarning` is the warning a `RepoBackend` gives before it
sends an `Authorization` header over plain `http://` (see `GitHubPath`).

## Command line

### `uripath` (`pathlib_next.tools.uripath`, URI layer)

```python
def main(argv=None, *, stdin=None, stdout=None, stderr=None) -> int: ...
def build_parser() -> argparse.ArgumentParser: ...
```

Console script `uripath` = `pathlib_next.tools.uripath:main`.
`main()` returns the
exit status and never raises `SystemExit`; `stdin` and `stdout` are binary
streams, `stderr` is a text or a binary stream (defaults: the process's), and
the usage and help text of the parser go to the process's own stderr and stdout;
`build_parser()` returns the `argparse` parser. An argument with `://`, or with a
scheme some class registers (`data:`, `zip:`, ...), is a `UriPath`; everything
else (including `C:/x` and `notes:draft`) is a `LocalPath`. `-` is stdin/stdout
where bytes are read or written. Without the `uri` extra local paths still work
and a URI argument fails with `the '<scheme>' scheme needs the 'uri' extra`
(the argument itself is never echoed, since it can carry a password).

| Subcommand | Arguments and flags |
| --- | --- |
| `read PATH` | Copy `PATH`'s bytes to stdout in chunks. |
| `write PATH [DATA] [--encoding utf-8]` | Write `DATA` encoded, or stdin's bytes when omitted. |
| `rm PATH [-r/--recursive] [--missing-ok] [--ignore-error]` | `Path.rm()`. |
| `cp SOURCE TARGET [-r/--recursive] [--overwrite] [--no-follow-symlinks] [--no-preserve-metadata]` | `Path.copy()`; with `-`, streams (an existing target needs `--overwrite`; no `-r`). |
| `sync SOURCE TARGET [--dry-run] [--remove-missing] [--size-only] [-v/--verbose] [--no-follow-symlinks]` | `PathSyncer` with content comparison; `--size-only` compares sizes. `--dry-run` prints `would copy SRC -> DST`/`would remove`/`would mkdir`/`would replace`/`would symlink`; `-v` prints the changes made. |

Exit status: 0 success (also `--help`); 1 the operation failed, with one line
`uripath: <Type>: <message>` on stderr; 2 a wrong invocation, with the usage on
stderr (a missing or unknown argument, `cp -r` with `-`, an unknown
`--encoding`); 130 Ctrl-C; 141 the reader of stdout went away, quietly (on
Windows too, where a closed pipe reports `EINVAL`).

## Environment variables

The core reads none. The URI layer reads one.

### `PATHLIB_NEXT_SFTP_BACKEND` (URI layer)

`auto` (the default when unset), `asyncssh` or `paramiko`: the SSH library
`SftpPath` uses when neither `backend=` nor a subclass's `_default_backend_cls`
names a backend. `auto` takes asyncssh when it is importable, else paramiko. The
value is case-sensitive, and an empty or unrecognised one raises `ValueError`. A
named library that is not installed raises `ImportError` naming the extra. The
variable is read the first time a path needs its default backend and the
resolved class is kept for the life of the process, so changing it afterwards
has no effect.

No other variable is read by this package. The libraries behind the schemes
read their own variables and files (`requests`' proxy settings and
`~/.netrc`, `boto3`'s credential chain, `~/.ssh/config`).

## Gotchas

### Core

- A `str` argument to `is_relative_to()` is parsed standalone via
  `self.with_segments(other)`, which keeps per-instance state (a `MemPath`
  backend). Normalize strings the same way in subclasses: `type(self)(x)`
  drops that state.

- **A binding is not a symlink.** `is_junction()` (pathlib 3.12 parity:
  a Windows junction) and `is_mount()` (a mount point, bind mounts
  included) report a directory that is another tree's second NAME.
  Neither is a symlink: `is_symlink()` is False, `readlink()` says
  nothing, and a non-following stat calls it an ordinary directory — which
  is why a symlink check cannot protect a walk from one. `is_dir_binding()`
  is the pair, and is what `rm(recursive=True)` consults: it removes the
  binding itself rather than the contents behind it (`rmdir()` on a live
  mount fails loudly, which beats emptying the mounted filesystem).
  Default False everywhere; `LocalPath`/`FileUri` answer for real.

### URI layer

- `/` and `joinpath()` take a `str` as an **already-decoded path**: `?`,
  `#`, `%` and a leading `C:` are ordinary filename characters
  (`base / "cache?v=2"` names that file), which is what `iterdir()` builds.
  A `Uri`/`UriPath` argument keeps URI semantics and is the only form that
  can cross to another endpoint -- where a credential-bearing backend is
  dropped. Dot segments are removed from the joined result either way, but
  a `%2e%2e` in a `str` key is a literal name. An empty segment is a segment
  (RFC 3986 3.3): `base / "a//b"`, `Uri(base, "a//b")` and
  `base / Uri("a//b")` are all `base/a//b`, where `pathlib` collapses it --
  on an object store `d//y` and `d/y` are two keys.
