# `pathlib_next` — public API header

Header-file-style reference for the installed `pathlib_next` package: the
public exports with their signatures, defaults, contracts and gotchas, so the
package can be used without reading its source. The project overview is the
`README.md` shipped next to this file (`pathlib_next/README.md`); the rendered
documentation is at <https://jose-pr.github.io/pathlib-next/>. Every deliberate
behavioral difference from `pathlib.Path` is listed, with its rationale, at
<https://jose-pr.github.io/pathlib-next/divergences/>; this file states the
resulting contract.

## Install and imports

`pip install pathlib-next[<extras>]`. No required dependencies.

| Extra | Installs | Needed for |
| --- | --- | --- |
| *(none)* | — | `Path`, `LocalPath`, `MemPath`, `utils`, `testing`, `uripath` on local paths |
| `uri` | `uritools`, `netimps>=0.4.0,<0.5` | `pathlib_next.uri` and **every** URI scheme (`file:`, `data:`, `ftp(s):`, archives included) |
| `http` | `requests` + `uri` | `http(s):`, `dav(s):`, `github:`, `gitlab:`, `git:` |
| `sftp` | `paramiko` + `uri` | `sftp:` (paramiko backend) |
| `sftp-async` | `asyncssh` + `uri` | `sftp:` (asyncssh backend) |
| `s3` / `gs` / `az` | `boto3` / `google-cloud-storage` / `azure-storage-blob` + `uri` | `s3:` / `gs:` / `az:` |

`import pathlib_next` exposes `Path`, `Pathname`, `LocalPath`,
`PosixPathname`, `WindowsPathname`, `FileStat`, the protocols `Stat`, `Chmod`,
`BinaryOpen`, `FsPathLike`, the aliases `PathLike`/`PurePathLike`, the modules
`glob` (`utils.glob`) and `sync` (`utils.sync`), and `Uri`/`UriPath` **only
when `uritools` is importable** — without the `uri` extra those two names are
silently absent and `from pathlib_next.uri import UriPath` raises
`ModuleNotFoundError`. `MemPath` lives in `pathlib_next.mempath`;
`pathlib_next.testing` is never imported implicitly.

`pathlib_next.__version__` is the installed distribution's version
(`importlib.metadata`), `"0+unknown"` for a source tree that is not installed.
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

## Pure-path / I/O base (`pathlib_next.path`)

- **`Pathname`** — ABC for a pure (no I/O) path. Abstract: `segments`,
  `parts`, `parent`, `with_segments(*segments)`, `as_uri()`,
  `relative_to(other)`. Derived: `name`, `suffix`, `suffixes`, `stem`
  (suffix rules of the running interpreter), `with_name`/`with_stem`/
  `with_suffix` (`ValueError` for `""`, `.` or a separator; from 3.13
  `with_stem("")` also raises when the name has a suffix), `parents`,
  `is_relative_to(other)`, `joinpath(*args)`, `/` and `"prefix" / path`,
  `root`/`drive`/`anchor` (`root` is `"/"` when the first segment is empty;
  `drive` is `""`), `match(path_pattern, *, case_sensitive=None)` (pathlib's
  right-anchored per-segment match; empty pattern → `ValueError`),
  `full_match(pattern, *, case_sensitive=None)` (3.13 semantics: the root is
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
  - A `str` argument to `is_relative_to()` is parsed standalone via
    `self.with_segments(other)`, which keeps per-instance state (a `MemPath`
    backend). Normalize strings the same way in subclasses: `type(self)(x)`
    drops that state.
- **`Path(Pathname, Chmod, Stat, BinaryOpen)`** — base class for I/O paths.
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
  - `samefile(other_path)` — compares `(st_dev, st_ino)`; `NotImplementedError`
    when `stat()` lacks them (`LocalPath` uses pathlib's).
  - `_same_filesystem(other) -> bool` — override hook: whether `other`'s
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
  - `_node_key() -> (namespace, names) | None` — override hook for a type
    whose segments spell one node several ways (`MemPath`: `a.txt`,
    `/a.txt`, `/d/../a.txt`; an archive member: `zip:` and `archive:`
    spellings, a query). `namespace` is an object two paths share exactly
    when they resolve names in one tree (compared with `is`); `names` is the
    node's normalized position in it (`()` for the root). `copy()` and
    `move()` compare two paths that both answer it, whatever their classes.
    Default `None`: two paths of one type that `_same_filesystem()` places
    together are compared with `==` and `is_relative_to()`. No I/O.
  - `iterdir() -> Iterator[Self]` — **stub** (`NotImplementedError`); a
    listable `Path` must implement it (or, on `UriPath`, `_listdir()`/
    `_scandir()`).
  - `_scandir() -> Iterator[tuple[str, FileStat | None]]` — non-following stat
    per entry; default: `iterdir()` + one `stat(follow_symlinks=False)` per
    child. Consumed by `walk()`, `glob()`, `rm(recursive=True)` and
    `PathSyncer`; override it when the listing call already returns metadata.
    `None` means "unknown", never "missing". Never yield a name that is not
    one path component (`utils.is_safe_child_name()`: `""`, `.`, `..`, or one
    containing `/` or NUL): every URI scheme's listing skips such a name, the
    `UriPath` default included, and an override must too.
  - `glob(pattern, *, case_sensitive=None, include_hidden=True,
    recursive=None, dironly=None, recurse_symlinks=False, native=True,
    on_error=None, bound_loops=False)` —
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
  - `rglob(pattern, **same_kwargs)` — `glob(f"**/{pattern}", recursive=True)`.
    `pattern=None` is `glob(None)`. `LocalPath` raises the audit events
    `pathlib.Path.glob` and `pathlib.Path.rglob` with the arguments `pathlib`
    passes on the running version (3.13+ also raises `glob` for the call
    `rglob` stands for); `pattern=None` raises none.
  - `walk(top_down=True, on_error=None, follow_symlinks=False)` — drives
    `_scandir()`; its stats are trusted only with `follow_symlinks=False`.
    Without `follow_symlinks` a symlink to a directory and a Windows junction
    (`is_junction()`) are listed in `filenames` and not entered, as
    `pathlib.Path.walk()` does; with it they are entered, and nothing protects
    the walk from a loop (`LocalPath` included: its walk is this one).
    A listed name that is not one path component (`utils.is_safe_child_name()`)
    is left out of `dirnames`/`filenames`; `on_error`, when given, is called
    with a `ValueError` for it (`error.filename` is the directory).
  - `touch(mode=None, exist_ok=True)` — `FileExistsError` when `exist_ok=False`
    and the path exists; never truncates; creates with `open("x")` (falls
    back to `"w"` when `x` is unsupported); `chmod(mode)` only when `mode` is
    passed (unmasked); does not update an existing file's mtime.
    `LocalPath`/`FileUri` use pathlib's `touch()`.
  - `_mkdir(mode)` (stub) / `mkdir(mode=0o777, parents=False, exist_ok=False)`.
  - `unlink(missing_ok=False)`, `rmdir()` — stubs.
  - `rm(recursive=False, missing_ok=False, ignore_error=False, *,
    follow_symlinks=False, follow_binds=False)` — extension.
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
  - `rename(target)` — stub. Implementations return the new path.
  - `_symlink_to(target, target_is_directory=False)` (stub; receives a path
    object) / `symlink_to(target, target_is_directory=False, *, force=False)`
    — `force=True` unlinks an existing non-directory entry first (not atomic;
    never removes a directory). A `str` target goes through the overridable
    `_symlink_target()` and is stored verbatim; relative stays relative:
    `LocalPath` hands the text on untouched (`./t/` stays `./t/`, as
    `pathlib` stores it) except on Windows, which cannot resolve a relative
    target spelled with `/`, so the path class rewrites it there; `UriPath`
    does not parse it as a URI, and any other class normalizes it with
    `with_segments()`, which keeps per-instance state. Implemented by
    `LocalPath` and `SftpPath` only.
  - `copy(target, *, overwrite=False, follow_symlinks=True,
    preserve_metadata=True, recursive=False, ignore_error=None,
    progress=None) -> None`
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
  - `copy_into(target_dir, *, overwrite=False, follow_symlinks=True,
    preserve_metadata=True, recursive=False, ignore_error=None,
    progress=None) -> Path` — `copy(target_dir / self.name, ...)`: the same
    keywords and defaults as `copy()`, and the new path is returned (pathlib
    3.14 returns it too). A `str` directory is a path on this backend, as for
    `copy()`. `ValueError` for a path with no name. Present on every backend
    and version; on 3.14 it replaces stdlib's, which overwrote and copied
    trees by default.
  - `move_into(target_dir, *, overwrite=False) -> Path` — `move(target_dir /
    self.name, overwrite=overwrite)`, returning the new path.
  - `replace(target) -> Path` — `move(target, overwrite=True)`, returning
    `target` as a path. A file replaces a file and a directory an empty
    directory; a directory that holds anything raises `OSError(ENOTEMPTY)`
    and a file onto a directory `IsADirectoryError`, both before anything is
    removed. `LocalPath` keeps stdlib's `os.replace()`.
  - `move(target, *, overwrite=False)` — validates first (missing source →
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
- **`FsPathLike`** — `Protocol` with `__fspath__() -> str`.
  **`PathLike`** = `str | Path`; **`PurePathLike`** = `str | Pathname`.

## Local filesystem (`pathlib_next.fspath`)

- **`LocalPath`** — `pathlib.WindowsPath`/`PosixPath` (by `os.name`) with
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
- **`PosixPathname`** / **`WindowsPathname`** — pure classes over
  `PurePosixPath`/`PureWindowsPath` implementing `Pathname`.

## In-memory filesystem (`pathlib_next.mempath`)

- **`MemPath(*segments, backend=None)`** — `Path` over nested dicts; the
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
- **`MemPathBackend(dict)`** — storage: `dict` value = directory,
  `bytearray` (`MemFile`, carrying `mtime`, which `copy`, `deepcopy` and
  `pickle` keep on every version) = file. Pass one instance as `backend=` to
  share a tree; each root `MemPath()` otherwise gets its own.

## Protocols (`pathlib_next.protocols`)

- **`fs.FileStatLike`** — `st_mode`, `st_size`, `st_mtime`.
- **`fs.Stat`** — `stat(*, follow_symlinks=True)` (stub). Derives `lstat()`,
  `exists(*, follow_symlinks=True)`, `is_dir(*, follow_symlinks=True)`,
  `is_file(*, follow_symlinks=True)`, `is_symlink()`, `is_block_device()`,
  `is_char_device()`, `is_fifo()`, `is_socket()`; any `OSError`/`ValueError`
  from `stat()` reads as `False`.
- **`fs.Chmod`** — `chmod(mode, *, follow_symlinks=True)` (stub; every
  implementation normalizes through `utils.as_mode()`, so `"0755"` works),
  `lchmod(mode)`, `_chown(uid, gid, *, follow_symlinks=True)` (stub, receives a
  canonical pair) / `chown(uid=None, gid=None, *, follow_symlinks=True)` —
  `None` or `-1` leaves a field unchanged, `int` is an id, `str` a name; an
  all-unchanged call returns without touching the backend.
- **`io.BinaryOpen`** — `_open(mode="r", buffering=-1) -> binary IO` (stub).
  `open(mode="r", buffering=-1, encoding=None, errors=None, newline=None)`
  validates the mode like builtin `open()` (`ValueError`) and passes
  `_open()` a canonical `r`/`w`/`x`/`a` plus optional `+` (never `b`/`t`);
  text mode wraps in `TextIOWrapper` and closes the handle if wrapping fails.
  An `_open()` that cannot honor a mode raises `NotImplementedError`. Derives
  `read_bytes()`, `read_text(encoding=None, errors=None, newline=None)`,
  `write_bytes(data)`, `write_text(data, encoding=None, errors=None,
  newline=None)`, `copy(target, *, progress=None,
  chunk_size=shutil.COPY_BUFSIZE)` (`progress(bytes_copied, total_size |
  None)`; an empty file reports once).
- **`checksum.NativeChecksum`** — opt-in (not on `Path`):
  `checksum(algorithm="md5") -> str` MUST raise `NotImplementedError` whenever
  it cannot return a genuine content digest under exactly that algorithm
  (never a different algorithm, never an ETag-like value); callers catch only
  `NotImplementedError`. `supported_checksums() -> frozenset[str]` (default
  empty) is advisory and never raises.

## URIs (`pathlib_next.uri`, `uri` extra)

- **`Uri(*uris, **options)`** — pure RFC 3986 URI, parsed lazily. Arguments
  (`str`, `bytes`, `Uri`, `pathlib`/`pathlib_next` paths, `os.PathLike`) join
  right to left like `joinpath` (an absolute one restarts); this is not RFC
  3986 reference resolution (`Uri("http://h/d") / "x"` is `http://h/d/x`),
  but the dot segments of the joined path are removed as RFC 3986 5.2.4
  says: `Uri("http://h/d/", "../x")`, `Uri("http://h/d/") / "../x"`,
  `Uri("http://h/d/") / Uri("../x")` and `Uri("http://h/d/../x")` are all
  `http://h/x`, and `Uri("http://h/d/x") / ".."` is `http://h/d/`. A `..`
  that would pass the root of an absolute path is dropped; a relative path
  keeps its leading `..` (`Uri("a") / "../../x"` is `../x`). A
  percent-encoded dot segment (`%2e%2e`) is removed after decoding. An
  absolute local path becomes `file:`; a relative one joins like a
  `PurePath`.
  - `/` and `joinpath()` take a `str` as an **already-decoded path**: `?`,
    `#`, `%` and a leading `C:` are ordinary filename characters
    (`base / "cache?v=2"` names that file), which is what `iterdir()` builds.
    A `Uri`/`UriPath` argument keeps URI semantics and is the only form that
    can cross to another endpoint -- where a credential-bearing backend is
    dropped. Dot segments are removed from the joined result either way, but
    a `%2e%2e` in a `str` key is a literal name.
  - Properties: `source -> Source`, `path -> str` (percent-decoded),
    `query -> Query` (**percent-encoded as received**, sent unchanged),
    `fragment -> str`, `parts -> (source, path, query, fragment)` (not path
    segments; use `segments`), `normalized_path`, `segments`, `parent`
    (`http://h/a` → `http://h/`; a trailing `/` is kept, so
    `Uri("http://h/d/").name == ""`).
  - Methods: `as_uri(sanitize=False)`, `with_source()`, `with_path()`,
    `with_segments()`, `with_query(str | mapping | pairs)` (a `str` is taken as
    already encoded), `with_fragment()`; `with_name`/`with_suffix`/`with_stem`
    keep query and fragment. `is_absolute()` (path starts with `/`),
    `is_relative_to(other)`, `relative_to(other, *, walk_up=False)`
    (`s3://b`/`http://h` count as the root), `is_local()`
    (`Source.is_local()`), `as_posix()` (`user@host:path` when a host is
    present).
  - `str()`/`repr()` drop the password (`sftp://u:pw@h/p` → `sftp://u@h/p`);
    `as_uri(sanitize=False)` keeps it. Non-ASCII hosts render as IDNA.
  - `==`/`hash` use the URI text; equal to another `Uri` or a URI string,
    never to a non-URI `Pathname`.
  - `__fspath__()` — the path for a `file:` URI on this machine (a named host
    on Windows is a UNC path) or for a scheme with `_host_filesystem_path =
    True` (`sftp:`, whose path is meaningful on **its** host); otherwise
    `NotImplementedError`. `host_fspath()` — the latter only, never local.
- **`UriPath(*uris, schemesmap=None, findclass=False, backend=None,
  **options)`** — `Uri` + `Path`. The bare class (or `findclass=True`)
  returns the subclass registered for the scheme, or plain `UriPath` for an
  unknown scheme (its I/O raises `NotImplementedError`). Resolution: classes
  already imported → entry point in group `pathlib_next.schemes` → built-in
  `pathlib_next.uri.schemes.*` module. An explicit `schemesmap` is the only
  map consulted.
  - Registering: `__SCHEMES = ("myscheme",)` in the class body (name-mangled:
    redeclare per class; the class name must not start with `_`). Defining or
    importing the subclass is enough, including after the first dispatch.
  - `backend` — per-instance connection state from `_initbackend()` (base:
    `None`), built on first use. A derived path (`/`, `joinpath()`,
    `parent`, `parents`, `with_name()`, `with_suffix()`, `with_query()`,
    `relative_to()`, `UriPath(base, x)`, a `Uri`/`PurePath` join) never
    builds one -- so it never imports an I/O library either -- and takes the
    backend its source path already holds, or none.
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
- **`Source(scheme, userinfo, host, port)`** (`uri.source`) — `NamedTuple`,
  falsy when all fields are empty. `as_str(sanitize=True)`; `str()`/`repr()`
  redact the password (the fields keep it). `Source.from_str(source,
  strict=True)` (`ValueError` for a path/query/fragment when strict),
  `parsed_userinfo() -> (user, password)` (`""` when absent),
  `get_scheme_cls(schemesmap=None) -> type[UriPath]`, `is_local()` —
  `localhost`/empty host or an address of this machine (IP literal, or any
  A/AAAA answer via `netimps`); cached per `Source` (`lru_cache(256)`), does
  DNS on a miss.
- **`Query(query, *, encoding="utf-8", separator="&")`** (`uri.query`) —
  `str` subclass holding the encoded query; built from a `str` (taken as
  encoded), a mapping (a sequence value repeats the key) or `(key, value)`
  pairs. `decode() -> list[tuple[str, str | None]]`, iteration yields the
  decoded pairs, `to_dict(*, single=False)`.

## Built-in schemes (`pathlib_next.uri.schemes`)

Every class is dispatched by `UriPath(...)`; `pathlib_next.uri.schemes`
re-exports `FileUri`, `DataUri`, `HttpPath`, `DavPath`, `FtpPath`, `SftpPath`,
`S3Path`, `GsPath`, `AzPath`, `GitHubPath`, `GitLabPath`, `GitPath`, `ZipUri`,
`TarUri` lazily (a name whose extra is missing is absent). Backends are passed
as `UriPath(uri, backend=...)` or `path.with_backend(...)`. Unsupported
operations raise `NotImplementedError`. Network errors map to pathlib types
(`FileNotFoundError`, `PermissionError`, `FileExistsError`,
`IsADirectoryError`, `NotADirectoryError`, `OSError(ENOTEMPTY)`), timeouts to
`TimeoutError`, anything else to `OSError`; transport exceptions are not
chained (their text can carry credentials).

- **`FileUri`** (`file:`; `schemes.file`) — `file:///abs`, `file:rel`,
  `file://localhost/C:/x`. `filepath -> LocalPath`; all I/O delegates to it
  (listing reuses `LocalPath`'s scandir). `rename()` accepts local targets
  only and returns a `FileUri`. No `symlink_to()`/`readlink()`. On Windows a
  drive is the anchor: `parents` ends at the drive root (`file:/C:/`) and
  equals the repeated `parent`, dot segments never climb above it, and a
  `str` join key or `copy()`/`move()`/`rename()` destination reads `\` as a
  separator (`C:\Temp\x` is an absolute path). Elsewhere a backslash is a
  filename character, and every other scheme splits on `/` only.
- **`DataUri`** (`data:`; `schemes.data`) — RFC 2397
  `data:[<mediatype>][;base64],<data>`. `mediatype` property (default
  `text/plain;charset=US-ASCII`). Read-only single file: `open("r")` only,
  `stat().st_size` is the decoded size, listing → `NotADirectoryError`.
- **`HttpPath`** (`http:`/`https:`; `http` extra; `schemes.http`)
  - `with_session(session, write_method="PUT", append_mode="rewrite",
    **requests_args) -> HttpPath` — installs `HttpBackend(session,
    requests_args, write_method, append_mode)` (a `NamedTuple` with
    `request(method, uri, **kwargs)`); `requests_args` (`headers=`, `auth=`,
    `verify=`, `timeout=`, ...) go to every request, request-specific
    headers merge over them.
  - Timeout: `DEFAULT_TIMEOUT = (10, 60)` (connect, read) unless given;
    `timeout=None` waits forever.
  - Redirects: `GET`/`HEAD`/`OPTIONS`/`PROPFIND` follow them as `requests`
    does. Every other request (`write_method`, `PATCH`, `DELETE`, `MKCOL`,
    `MOVE`) is sent with `allow_redirects=False`, and an `allow_redirects` in
    `requests_args` or the call does not change that: a 307/308 to the same
    scheme, host and port is re-sent once with the same method and body; any
    other 3xx, another origin or a second redirect raises `OSError(EIO)`
    naming the status and the `Location` (userinfo removed). A body that is
    not bytes or `str` cannot be re-sent, so it raises too.
  - URL userinfo is sent as Basic `auth=` (not in the URL) unless
    `requests_args`/`session.auth` set auth; it takes priority over `~/.netrc`.
  - `stat(*, follow_symlinks=True, walk_up_last_modified=False)` — `HEAD`
    (`GET` on 405); a final URL ending in `/` is a directory; `st_size` from
    `Content-Length`, `st_mtime` from `Last-Modified` (UTC), or from the
    parent's index when `walk_up_last_modified=True`.
  - Listing scrapes an Apache/nginx-style HTML index; `.`/`..` rows are never
    children; a non-HTML response → `NotADirectoryError` (an HTML file lists
    as empty). Cannot always tell a file from an index page.
  - `open("r")` streams `GET` with `Accept-Encoding: identity`. `"w"`/`"x"`
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
- **`DavPath(HttpPath)`** (`dav:`/`davs:`, sent as `http:`/`https:`; `http`
  extra; `schemes.dav`) — same backend and `with_session()`. `stat()`/
  listing via `PROPFIND`. `open("r")` on a collection → `IsADirectoryError`;
  `"w"`/`"x"` `PUT` on close (`"x"` as for `HttpPath`, probing with
  `PROPFIND`); `"a"` unsupported. `mkdir()` = `MKCOL`
  (missing parent → `FileNotFoundError`). `unlink()` refuses a collection;
  `rmdir()` checks emptiness first; `rm(recursive=True)` is one recursive
  `DELETE` (failed members of a 207 raise). `rename()` = `MOVE` with
  `Overwrite: F` (existing target → `FileExistsError`), no credentials in
  `Destination`. 423 → `PermissionError`. `PUT`, `MKCOL`, `DELETE` and `MOVE`
  follow the `HttpPath` redirect rule above; `PROPFIND` follows redirects.
  No `chmod()`.
- **`FtpPath`** (`ftp:`/`ftps:`; `uri` extra; `schemes.ftp`)
  - `FtpBackend(timeout=30.0, ssl_context=None, verify=True)` — `timeout`
    bounds connect, replies and transfers (`None` = forever). `ftps:` is
    explicit TLS with `PROT P`; the certificate and host name are verified
    with `ssl.create_default_context()`; `ssl_context` (e.g.
    `ssl.create_default_context(cafile=...)`) wins over `verify`;
    `verify=False` accepts any certificate. Data connections reuse the TLS
    session. No user in the URI → anonymous login.
  - `BaseFtpBackend.client(source, tls) -> ftplib.FTP` — override to supply
    connections.
  - Paths without `backend=` share one default `FtpBackend()`. Connections
    are cached per (backend, source, tls, thread) (LRU of 128, closed on
    eviction), probed with `NOOP` and replaced when dead.
  - Listing/stat use `MLSD` (UTC `modify`; mode from `unix.mode` or the
    `perm` fact); servers without it fall back to `NLST`/`SIZE`. A listing is
    read whole inside the guarded call: a transient `4xx` reply is
    `OSError(EAGAIN)`, a name the client cannot decode is `OSError(EILSEQ)`,
    and a listing that dies mid-transfer drops the connection, so the next
    request reconnects.
  - An empty path (`ftp://host`) is the root, as `ftp://host/` is: `/` is
    sent in every command that takes a path, never an empty argument (which a
    server reads as its working directory).
  - Reads download the whole file into memory; `"w"`/`"x"`/`"a"` (`APPE`)/
    `"r+"` buffer in memory and upload on close (`"x"` checks then writes).
  - `rename()` on the same server. `chmod()` via `SITE CHMOD`
    (`NotImplementedError` when the server lacks it or
    `follow_symlinks=False`).
- **`SftpPath`** (`sftp:`; `sftp` or `sftp-async` extra; `schemes.sftp`)
  - `SftpPath(*uris, backend=None, ssh_config=<default>)` — `ssh_config`:
    default `~/.ssh/config`, `None` for none, a path or iterable of paths;
    inherited by derived paths. A host that is not a plain host name or
    address -- an IP address (an IPv6 one may carry a `%zone`), or ASCII
    letters, digits, `.`, `_`, `-` (not first) and the letters and digits of
    other scripts -- raises `ValueError` naming it where a connection or an
    ssh_config lookup would start, on both backends; building, joining and
    comparing such a path still work.
  - Backend selection, highest first: `backend=` → subclass attribute
    `_default_backend_cls` → env `PATHLIB_NEXT_SFTP_BACKEND`
    (`auto`|`asyncssh`|`paramiko`; a named backend that is not installed
    raises `ImportError`) → auto (asyncssh if importable, else paramiko).
  - **`SftpBackend(connect_opts=None, hostkeypolicy=None,
    ssh_config=<default>, *, known_hosts=<default>, timeout=30.0)`**
    (paramiko). Host keys are verified: `known_hosts` default is
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
    shell metacharacter or a leading `-` is refused when `%r` is used); an
    `Include` inside a `Host` or `Match` block applies the included file's
    blocks only where the enclosing one does. Connections cached per
    (backend, source, thread), replaced when dropped.
  - **`AsyncsshSftpBackend(connect_opts=None, *, max_concurrency=None,
    sftp_version=4, ssh_config=<default>, timeout=60.0)`**. asyncssh verifies
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
  - Both backends: `close()` closes every cached connection (the backend stays
    usable); `default(ssh_config=...)` classmethod.
    `BaseSftpBackend.client(source)` is the override point
    (`supports_lchmod`, `supports_hardlink`, `checksum()`,
    `supported_checksums()`).
  - `readlink() -> SftpPath` (verbatim target) and `symlink_to()` on both
    backends; `hardlink_to(target)` and `chmod(follow_symlinks=False)` on
    asyncssh only (paramiko → `NotImplementedError`); `chown()` with numeric
    ids only.
    `rename()` replaces an existing target via `posix-rename@openssh.com`
    where supported, else `FileExistsError`. `checksum()`/
    `supported_checksums()` (`NativeChecksum`): both backends send the
    `check-file-handle` extension (OpenSSH lacks it); a refusal is remembered
    per connection; every failure is `NotImplementedError`. `__fspath__()`/`host_fspath()`
    return `.path`.
- **`S3Path`** (`s3://bucket/key`; `s3` extra; `schemes.s3`) — `bucket`,
  `key` (one trailing `/` dropped: `s3://b/dir/` is `dir`).
  `S3Backend(**client_kwargs)` → one lazily built, thread-shared
  `boto3.client("s3", **client_kwargs)` (default: boto3's own credential and
  endpoint configuration); `BaseS3Backend.client()` is the override point.
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
- **`GsPath`** (`gs://bucket/key`; `gs` extra; `schemes.gs`) — `bucket_name`,
  `key`. `GsBackend(**client_kwargs)` → `google.cloud.storage.Client(
  **client_kwargs)` unchanged (emulator: `client_options={"api_endpoint":
  url}, use_auth_w_custom_endpoint=False`, or set `STORAGE_EMULATOR_HOST`
  yourself); `BaseGsBackend.client()`. Same prefix model, rename and
  recursive-copy/remove rules as `S3Path` (same bucket); `rmdir()` of the
  bucket root → `PermissionError`; reads load the whole object; `"x"` uses
  `if_generation_match=0`; `"a"` unsupported; `st_mtime` from `updated`.
- **`AzPath`** (`az://account/container/key`; `az` extra; `schemes.az`) —
  `account`, `container`, `key` (one trailing `/` dropped, interior `//`
  kept). `AzBackend(account=None, **client_kwargs)`: `connection_string=` →
  `BlobServiceClient.from_connection_string(...)`; otherwise kwargs go to
  `BlobServiceClient` and `account` alone derives
  `account_url="https://<account>.blob.core.windows.net"` with
  `azure-identity`'s `DefaultAzureCredential` unless `credential=` is passed.
  Without `backend=`, one shared backend per URI account is used, which needs
  `azure-identity` (installed by the `az` extra; `ImportError` otherwise). Same
  prefix model and recursive-copy/remove rules as `S3Path` (a name that is
  both a blob and a prefix is the blob, whatever order the SDK lists them
  in); `rmdir()` of the container root → `PermissionError`; `rename()` within
  one container; `"x"` sends
  `If-None-Match: *`; `"a"` unsupported; `st_mtime` from `last_modified`.
- **`GitHubPath`** (`github://[TOKEN@]host/owner/repo/path?ref=REF`; `http`
  extra; `schemes.github`) — read-only; `open()` other than `"r"` and every
  write method raise `NotImplementedError`.
  - Properties: `owner`, `repo`, `repo_path`, `ref` (`None` = default
    branch); `?ref=` is carried to every child.
  - `RepoBackend(token=None, session=None, api_base=None, **requests_args)`
    (`schemes._gitrepo`): `Authorization: Bearer <token>`, timeout default
    `(10, 60)`, `cache` dict; `api_base` overrides the API root;
    `BaseRepoBackend.request(method, url, **kwargs)` is the override point.
    Without `backend=`, the token is the userinfo password
    (`x-access-token:TOKEN@`) or else the bare user (`TOKEN@`).
  - `str()`, `repr()` and `as_uri(sanitize=True)` drop the whole userinfo.
  - API root `https://api.github.com` for `github.com`, else
    `https://host[:port]/api/v3`. Contents API listings (a directory at the
    1,000-entry cap is re-read through the Git Trees API); file bodies use
    the raw media type; symlink/submodule entries read as files;
    `st_mtime` is `0`.
  - Rate limits (403/429 with limit headers, any 429) → `OSError(EAGAIN)`.
- **`GitLabPath`** (`gitlab://[TOKEN@]host[:port]/owner/repo/path`, or
  `.../group/sub/project/-/path` — a `-` segment at position 3 or later is
  the separator; `schemes.gitlab`) — same backend, properties and read-only
  contract; API root `https://host[:port]/api/v4` (`gitlab.com` by
  default). Without `?ref=` the default branch is fetched once
  (`GET /projects/:id`) and cached in `backend.cache`. Tree listings are
  paginated (100 per page); file entries carry no stat hint (a `stat()` per
  file); `st_mtime` is `0`.
- **`GitPath`** (`git:`; `schemes.git`) — `git://github.com/...` constructs a
  `GitHubPath`, `git://gitlab.com/...` a `GitLabPath`; any other host →
  `ValueError`. `git+github:` (`GitHubGitPath`) and `git+gitlab:`
  (`GitLabGitPath`) pin the provider for any host.
- **Archives** (`schemes.archive`): `ZipUri` (`zip:`), `TarUri` (`tar:`),
  `ArchiveUri` (`archive:`, detects the format from the outer name
  `.zip`/`.jar` vs `.tar`/`.tgz`/`.tar.*`, else a `PK` magic sniff),
  `ArchiveZipUri` (`archive+zip:`), `ArchiveTarUri` (`archive+tar:`).
  - Syntax `<scheme>:<archive-uri>!/<member>`; `<archive-uri>` must carry a
    scheme (`ValueError` otherwise) and may be any URI, including another
    archive (each leading archive scheme consumes one `!/`; nested archives
    are read-only). A member name containing `!/` is written `%21/`.
    `name`/`parent`/`glob()` work on the member path; `as_uri()`
    percent-encodes it.
  - One shared handle per archive (keyed by the real local path, or the outer
    URI), released when no path references it. A non-local outer is read into
    memory.
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
    `zipfile.BadZipFile` / `tarfile.ReadError`, as a listing does.
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
    nothing.
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

## CLI (`uripath`, `pathlib_next.tools.uripath`)

Console script `uripath` = `pathlib_next.tools.uripath:main`.
`main(argv=None, *, stdin=None, stdout=None, stderr=None) -> int` (streams are
binary; defaults are the process streams); `build_parser() ->
argparse.ArgumentParser`. An argument with `://`, or with a scheme some class
registers (`data:`, `zip:`, ...), is a `UriPath`; everything else (including
`C:/x` and `notes:draft`) is a `LocalPath`. `-` is stdin/stdout where bytes
are read or written. Without the `uri` extra local paths still work and a URI
argument reports the extra to install.

| Subcommand | Arguments and flags |
| --- | --- |
| `read PATH` | Copy `PATH`'s bytes to stdout in chunks. |
| `write PATH [DATA] [--encoding utf-8]` | Write `DATA` encoded, or stdin's bytes when omitted. |
| `rm PATH [-r/--recursive] [--missing-ok] [--ignore-error]` | `Path.rm()`. |
| `cp SOURCE TARGET [-r/--recursive] [--overwrite] [--no-follow-symlinks] [--no-preserve-metadata]` | `Path.copy()`; with `-`, streams (an existing target needs `--overwrite`; no `-r`). |
| `sync SOURCE TARGET [--dry-run] [--remove-missing] [--size-only] [-v/--verbose] [--no-follow-symlinks]` | `PathSyncer` with content comparison; `--size-only` compares sizes. `--dry-run` prints `would copy SRC -> DST`/`would remove`/`would mkdir`/`would replace`/`would symlink`; `-v` prints the changes made. |

Errors print `uripath: <Type>: <message>` to stderr and return 1; a closed
stdout returns 141, Ctrl-C 130.

## Testing helpers (`pathlib_next.testing`, needs `pytest`)

- **`FIXTURE_TREE`** — `{"a.txt": "a", "b.py": "b", ".hidden.txt": "hidden",
  "sub": None, "sub/c.py": "c", "sub/nested": None, "sub/nested/d.py": "d",
  "empty_dir": None}` (`None` = directory).
- **`populate_fixture_tree(root) -> root`** — builds `FIXTURE_TREE` under an
  existing empty directory through the path's own `mkdir()`/`write_text()`
  (any `Path`, or a stdlib `pathlib.Path`).
- **`PurePathContract`** — name/suffix/stem, parents, join, `match()`; needs a
  `root` fixture only.
- **`ReadPathContract(PurePathContract)`** — exists/types, reads and read
  modes, `iterdir()`, `stat()`, `glob()`/`rglob()`, `walk()`, with pathlib's
  exception types. Capability attributes: `supports_listing`,
  `supports_empty_directories`, `distinguishes_file_types`.
- **`PathContract(ReadPathContract)`** — `mkdir()`, writes and write/append/
  exclusive modes, `unlink()`, `rmdir()`, `rm()` (also with
  `follow_symlinks=`/`follow_binds=`: an implementation that overrides
  `rm()` must accept both), `copy()` (recursive),
  `move()`, `rename()`, `touch()`, copying a file onto a separately built
  spelling of itself (`OSError`, content kept) and `_same_filesystem()`
  within one root. Capability attributes: `supports_rename`,
  `supports_append`, `supports_exclusive_create`,
  `enforces_directory_hierarchy`.
- Both I/O contracts need `root` to be a **fresh, function-scoped** directory
  populated with `populate_fixture_tree()`; two contract classes must not
  share one. Capability attributes default to `True`; setting one `False`
  makes the tests it covers skip. `DIRECTORY_ERRORS = (IsADirectoryError,
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

## Utilities (`pathlib_next.utils`)

- **`glob`** — `glob.glob(path, *, dironly=False, root_dir=None,
  recursive=False, include_hidden=False, case_sensitive=None)`: the pattern
  is itself a path (`UriPath("file:/x/**/*.py")`); like stdlib `glob`, hidden
  names need `include_hidden=True`. It is split at the first wildcard, never
  in the path's anchor (a drive, `\\?\C:\`); a bad pattern raises when it is
  called, the selection is lazy. `glob.parse_pattern(pattern) ->
  (parts, trailing_sep)` (`ValueError`/`NonRelativePatternError`),
  `glob.select(base, parts, *, dironly=False, recursive=True,
  include_hidden=True, case_sensitive=None)` (the engine behind
  `Path.glob()`), `glob.full_match(segments, pattern, case_sensitive)`,
  `glob.NonRelativePatternError(NotImplementedError, ValueError)`,
  `glob.RECURSIVE = "**"`.
- **`sync.PathSyncer(checksum=None, /, remove_missing=False,
  follow_symlinks=True, symlink_mode="preserve", hook=None,
  ignore_error=False, quick_check=True)`** — one-way tree sync between any two
  `Path` implementations.
  - `.sync(source, target, /, dry_run=False, ignore_error=None)` — `None` uses
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
  - **`SyncEvent`** members: `Copy`, `RemovedMissing`, `Synced`,
    `CreatedDirectory`, `SyncStart`, `TypeMismatch`, `CheckTargetChild`,
    `CheckTargetChildren`, `SyncChild`, `SyncChildren`, `Symlink`, `Compare`
    (a comparison failed; only passed to `ignore_error`), `Skipped` (not a
    file, directory or link), `Error` (an error was tolerated).
  - **`sync.PathAndStat(path, *, follow_symlink=True)`** — `path` plus cached
    `stat` (`FileStat | None`); `from_stat(path, stat)`, `exists()`,
    `refresh(follow_symlink=True)`; `is_*` attributes delegate to the stat
    (always-`False` callables when missing).
- **`stat.FileStat(st_mode=None, st_size=0, st_mtime=0, is_dir=False)`** —
  slotted stat for non-`os` backends. Without `st_mode`, a placeholder
  (`S_IFREG|0o444` / `S_IFDIR|0o555`) with `mode_known=False`. A bare
  permission `st_mode` (`0o644`) gets the type bits of `is_dir` ORed in and
  counts as reported; one that carries a type keeps it.
  `from_stat(stat)` (a `FileStat` passes through; `None` fields become 0),
  `from_path(path, *, follow_symlink=True) -> FileStat | None` (`None` only
  on `FileNotFoundError`), `settime(value)`, `setmode(value, isdir=None)`,
  `items()`. `is_dir()`/`is_file()`/... are **methods**.
- **`checksum`** — `md5(path, chunk_size=65536)`, `sha256(path,
  chunk_size=65536)`, `stream(path, algorithm="md5", chunk_size=65536)`
  (streamed through `open("rb")`, `usedforsecurity=False`),
  `native(path, algorithm="md5") -> str | None` (`None` when the protocol is
  missing or raises `NotImplementedError`). `md5`/`sha256` are also
  importable from `pathlib_next.utils`.
- **`archive`** — `make_archive(src, format, target)` (`format` `"zip"` or
  `"tar"`, else `ValueError`; `src` file or directory of any `Path`; built in
  a temporary buffer, written to `target` only when complete; zip64 always)
  and `unpack_archive(archive, dest)` (format from the name, else magic
  bytes; creates `dest`; non-seekable streams are buffered; members that
  would leave `dest` are skipped, and for a Windows-flavoured `dest` so are
  members with a part Windows would rewrite or send to a device
  (`UserWarning`); tar hard links and links to regular files are extracted as
  copies, other links skipped with `UserWarning`). Also importable from
  `pathlib_next.utils`.
- **`is_safe_child_name(name, *, windows=False) -> bool`** — `False` for a
  non-`str`, `""`, `.`, `..`, or a name containing `/` or NUL; with
  `windows=True` also `\`, `:`, a trailing dot or space, and the reserved
  device names `CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9` (any
  case, with or without an extension: `nul.txt`).
  **`is_windows_flavoured(path) -> bool`**
  — `True` for a `PureWindowsPath` or a path whose `filepath` is one.
- **`LRU(func, maxsize=128, on_evict=None)`** — thread-safe memoizing cache,
  called like `func`. `on_evict(key_tuple, value)` runs for every dropped
  value (overflow, `maxsize` shrink, `invalidate`/`discard`, a losing
  concurrent miss), outside the lock, exceptions suppressed. `discard(*args)
  -> bool`, `invalidate(*args)` (discard + recompute), settable `maxsize`.
  `maxsize=None` is unbounded; `maxsize=0` (or less) stores nothing, so every
  call runs `func` and its result never reaches `on_evict`.
- **`as_mode(mode) -> int`** — `int` passes through; `str` is octal (optional
  `0o`), any non-octal digit → `ValueError`. A `bool` → `TypeError`; a number
  below 0 or above `0o177777` (the 16 bits of a `st_mode`, type bits
  included) → `ValueError`.
- **`as_owner(uid, gid) -> (int | None, int | None)`** — `-1` → `None`;
  `str` names pass through; a `bool` → `TypeError`, an id outside
  0..2**32-1 → `ValueError`. **`UNCHANGED = None`**.
- **`as_error_handler(ignore_error, *, default=False) -> callable`** — a
  callable passes through untouched (arity is the call site's: `rm` `(error,
  path)`, `copy` `(error)`, `PathSyncer` `(error, source, target, event)`);
  a bool (or `None` → `default`) becomes a constant-returning callable.
- **`notimplemented(method)`** — decorator; calling raises
  `NotImplementedError("Method not implemented: <name>")`.
- **`sizeof_fmt(num) -> str`** — `1536` → `"1.5K"`.
- **`parsedate(date) -> float | int`** — UTC epoch seconds from an HTTP date
  string (zone offset applied, none = UTC), a `struct_time`/tuple (UTC minus
  any offset), or a number (returned unchanged); `None` or an unparseable
  string → `0`. `bytes`, a `datetime` and a tuple of fewer than six items are
  not accepted (`ValueError`/`TypeError`), and a date that does not exist
  (31 Feb) rolls over instead of being refused.
