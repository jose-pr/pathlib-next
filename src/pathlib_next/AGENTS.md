# `pathlib_next` — public API header

Header-file-style reference for the `pathlib_next` package: every public
export with its signature, arguments, contract, and gotchas, so this module
can be consumed without reading its source. Kept current with the public
API. For the project overview, install extras, and code layout, see the
repo-root `AGENTS.md`. Any behavioral divergence from `pathlib.Path` is
recorded in `docs/divergences.md` — this file documents the *contract*, not
every internal deviation.

`import pathlib_next` re-exports `path`, `fspath`, `utils.glob`,
`utils.sync`, and (if `uritools` is importable) `uri.Uri`/`uri.UriPath`; a
missing `uritools` degrades that last import silently (`try`/`except
ImportError: pass`), so `pathlib_next.uri` may need an explicit
`from pathlib_next.uri import UriPath` even after a plain `import
pathlib_next`.

## Pure-path / I/O base (`pathlib_next.path`)

- **`Pathname`** — ABC for a pure (no I/O) path: `name`, `suffix`,
  `suffixes`, `stem`, `segments` (abstract), `parts` (abstract),
  `with_segments(*segments)` (abstract), `with_name`/`with_stem`/
  `with_suffix`, `relative_to(other)`, `is_relative_to(other)`,
  `__truediv__`/`joinpath`, `root`/`drive`/`anchor` (all `""` unless
  overridden), `parent`/`parents` (abstract `parent`), `is_absolute()`
  (abstract), `match(pattern, *, case_sensitive=None)`,
  `full_match(pattern, *, case_sensitive=None)`, `as_posix()`,
  `has_glob_pattern()`. `as_uri()` is abstract on `Pathname` itself.
- **`Path(Pathname, Chmod, Stat, BinaryOpen)`** — base class for I/O paths.
  `Path(*args)` (the bare class, not a subclass) always constructs a
  `LocalPath` (`fspath.py`) — the real local filesystem. Adds:
  - `is_hidden()` — name starts with `"."`.
  - `samefile(other_path)` — compares `(st_dev, st_ino)` from `stat()`;
    raises `NotImplementedError` if either isn't available (`LocalPath` gets
    a real implementation from `pathlib.Path` via MRO instead).
  - `iterdir() -> Iterator[Self]` — **not implemented** by default (raises
    `NotImplementedError`); every concrete `Path` overrides it.
  - `_scandir() -> Iterator[tuple[str, FileStat | None]]` — default falls
    back to `iterdir()` + one `stat()` per child; override directly when the
    listing call already returns metadata (used by `walk()`/`glob()` so
    remote schemes avoid a stat round trip per entry).
  - `glob(pattern, *, case_sensitive=None, include_hidden=False,
    recursive=None, dironly=None)` — a `"**"` pattern component
    auto-enables recursion (pathlib parity); pass `recursive=False`
    explicitly to disable it even with `"**"` present, or `True` to force it
    without `"**"`. A recursive glob on a remote scheme walks the whole
    subtree, one round trip per directory.
  - `rglob(pattern, ...)` — `glob(f"**/{pattern}", recursive=True)`.
  - `walk(top_down=True, on_error=None, follow_symlinks=False)` — drives
    `_scandir()`, not `iterdir()`; the pre-seeded stat from `_scandir()` is
    trusted only when `follow_symlinks=False` (its own default) — an
    explicit `follow_symlinks=True` always re-`stat()`s each entry.
  - `touch(mode=0o666, exist_ok=True)` — raises `FileExistsError` (not a
    silent truncate) when `exist_ok=False` and the file exists.
  - `_mkdir(mode)` (not implemented by default) / `mkdir(mode=0o777,
    parents=False, exist_ok=False)` — `mkdir()` retries through
    `_mkdir()`, creating parents on `FileNotFoundError` when `parents=True`.
  - `unlink(missing_ok=False)` / `rmdir()` — not implemented by default;
    every concrete `Path` overrides them.
  - `rm(recursive=False, missing_ok=False, ignore_error=False |
    Callable[[Exception, Self], bool])` — extension, no direct pathlib
    equivalent. Removes a file or (with `recursive=True`) a directory tree;
    `ignore_error` (bool or predicate) controls whether an error during the
    walk is swallowed (predicate return `True`) or re-raised.
  - `rename(target)` — not implemented by default.
  - `copy(target, *, overwrite=False, follow_symlinks=True,
    preserve_metadata=True, recursive=False, ignore_error=None)` —
    `follow_symlinks`/`preserve_metadata` names match CPython 3.14's
    `Path.copy()`; `overwrite` is this library's own extension (3.14 always
    raises if the destination exists). `preserve_metadata` defaults `True`
    here (3.14 defaults `False`) and only preserves `st_mode`, not
    timestamps/xattrs. `ignore_error`, when given, receives exceptions
    instead of raising (same contract as `rm()`'s callable form); `None`
    (default) fails on the first error.
  - `move(target, *, overwrite=False)` — tries `rename()` first, falls back
    to `copy(recursive=True)` + `rm(recursive=True)`/`unlink()` when
    `rename()` raises `NotImplementedError`.
- **`PathLike`** — `Union[str, Path]`. **`PurePathLike`** — `Union[str,
  Pathname]`. **`FsPathLike`** — `Protocol` requiring `__fspath__() -> str`.

## Local filesystem (`pathlib_next.fspath`)

- **`LocalPath`** — `pathlib.WindowsPath`/`PosixPath` (by `os.name`) with
  this library's `Path` mixed in via MRO. Behaves exactly like
  `pathlib.Path` for anything not explicitly overridden (see
  `docs/divergences.md`); overrides `_scandir()`, `walk()`, `stat()`,
  `chmod()`, and `glob()` to keep this project's contracts (tuple-yielding
  `_scandir`, `follow_symlinks=` support pre-3.10) regardless of what a given
  Python version's own `pathlib.Path` does at the same MRO position.
- **`PosixPathname`** / **`WindowsPathname`** — pure (no I/O) path classes
  implementing `Pathname` on top of `pathlib.PurePosixPath`/
  `PureWindowsPath`.

## In-memory filesystem (`pathlib_next.mempath`)

- **`MemPath(Path)`** — `MemPath(*segments, backend=None, **kwargs)`.
  In-memory path over nested dicts; a `dict` value is a directory, a
  `bytearray` value is a file's content. Reference exemplar for subclassing
  `Path` directly. `relative_to()` is not implemented. `as_uri()` returns
  `mempath:<url-quoted posix path>`. Supports `_open()` modes `"r"`, `"w"`,
  `"x"`, `"a"` (the `"a"` extension isn't part of the base `BinaryOpen`
  contract). `rename()` is not implemented (see the scheme feature matrix in
  the README).
- **`MemPathBackend(dict)`** — the nested-dict storage. Share one instance
  across `MemPath`s via `backend=` to give them the same virtual filesystem;
  omitted, each root `MemPath()` gets its own.

## Protocols (`pathlib_next.protocols`)

- **`fs.FileStatLike`** — `Protocol`: `st_mode`, `st_size`, `st_mtime`
  (all abstract properties).
- **`fs.Stat`** — `Protocol`. `stat(*, follow_symlinks=True) ->
  FileStatLike` (not implemented by default). Derives `lstat()`,
  `exists()`, `is_dir()`, `is_file()`, `is_symlink()`, `is_block_device()`,
  `is_char_device()`, `is_fifo()`, `is_socket()` — all methods, not
  properties. `exists()`/the `is_*` methods swallow `OSError`/`ValueError`
  from `stat()` and report `False` rather than propagating (pathlib parity).
- **`fs.Chmod`** — `Protocol`. `chmod(mode, *, follow_symlinks=True)` (not
  implemented by default); derives `lchmod(mode)`.
- **`io.BinaryOpen`** — `Protocol`. `_open(mode="r", buffering=-1) ->
  io.IOBase` (not implemented by default; must yield a **binary** stream).
  Derives `open(mode="r", buffering=-1, encoding=None, errors=None,
  newline=None)`, `read_bytes()`, `read_text(encoding=None, errors=None,
  newline=None)`, `write_bytes(data)`, `write_text(data, encoding=None,
  errors=None, newline=None)`, `copy(target)` (streams this object's binary
  content into another `BinaryOpen`).

## URIs (`pathlib_next.uri`)

Only importable if `uritools` is installed (the `uri` extra or any scheme
extra that depends on it).

- **`Uri(Pathname)`** — a pure (no I/O), RFC 3986 URI, lazily parsed into
  `source`/`path`/`query`/`fragment` on first access. `Uri(*uris,
  **options)` — multiple constructor args are joined pathlib-`joinpath`-style
  (right to left, stopping at the first absolute segment) — this is **not**
  RFC 3986 reference resolution, and `..` is never resolved during join (see
  `docs/divergences.md`). Properties: `source -> Source`, `path -> str`,
  `query -> str`, `fragment -> str`, `parts -> (source, path, query,
  fragment)`, `normalized_path` (posixpath-normalized `path`), `segments`,
  `suffix`, `stem`, `parent`. Methods: `as_uri(sanitize=False)` (sanitize
  strips password from userinfo before formatting), `with_source(source)`,
  `with_segments(*segments)`, `with_path(path)`, `with_query(query)`,
  `with_fragment(fragment)`, `is_absolute()`, `is_relative_to(other)`,
  `relative_to(other, *, walk_up=False)`, `is_local()` (delegates to
  `Source.is_local()` — does a DNS lookup, cached per `Source`),
  `as_posix()` (`user@host:path` / `host:path` form when a source is
  present). `__fspath__()` only succeeds for a `file:`-scheme URI pointing
  at this machine; otherwise raises `NotImplementedError`.
- **`UriPath(Uri, Path)`** — `Uri` + `Path` (I/O) + scheme dispatch.
  `UriPath(*uris, **options)` (the bare class) parses the URI and returns an
  instance of the concrete subclass registered for its scheme via
  `__SCHEMES` (name-mangled per class — declare `__SCHEMES = ("http",
  "https")` in the subclass body, not as a module-level or dynamically
  assigned attribute, and never give a `__SCHEMES`-registered class a
  leading underscore in its name, or the name-mangled lookup silently
  misses). If the scheme isn't loaded yet, resolution tries a
  `pathlib_next.schemes` entry point first, then imports the matching
  builtin `uri/schemes/*` module — importing any module that defines a
  `UriPath` subclass registers it. `backend` property — per-instance
  connection/session state, lazily created via `_initbackend()` (override
  in a scheme subclass; base returns `None`); `with_backend(backend)`
  returns a new instance sharing the given backend. `_listdir() ->
  Iterator[str]` (not implemented by default) / `_scandir()` (derives from
  `_listdir()` + one `stat()` per child unless overridden directly — prefer
  overriding `_scandir()` when the listing call already returns
  type/size/mtime metadata, e.g. WebDAV PROPFIND, FTP MLSD, SFTP
  `listdir_attr`, an S3 list page). `iterdir()` is provided (drives
  `_scandir()`); implement `_listdir()` or `_scandir()`, not `iterdir()`
  itself.
- **`Source`** (`uri.source`, re-exported at `uri.Source` via `uri/__init__`
  imports) — `NamedTuple(scheme, userinfo, host, port)`; falsy when every
  field is empty/`None`. `Source.from_str(source, strict=True) -> Source`
  (`strict=True` raises `ValueError` if `source` carries a path/query/
  fragment). `parsed_userinfo() -> (user, password)`. `get_scheme_cls(
  schemesmap=None) -> type[UriPath]` — resolves (and lazily loads) the
  scheme class. `is_local()` — DNS lookup, `lru_cache(maxsize=256)`d per
  `Source` value; never call on a hot path uncached.
- **`Query(str)`** (`uri.query`) — a URI query string, buildable from a
  `str`, a sequence of `(key, value)` pairs, or a mapping (`value` may be a
  sequence to repeat the key). `Query(query, *, encoding="utf-8",
  separator="&")`. `decode() -> list[tuple[str, str | None]]`,
  `__iter__()` (iterates decoded pairs), `to_dict(*, single=False) ->
  dict[str, list[str | None]]` (or `dict[str, str | None]` when
  `single=True`, last value wins).

Built-in scheme modules live under `uri/schemes/` — see the table in the
repo-root `AGENTS.md`. `PATHLIB_NEXT_SFTP_BACKEND` env var (`"paramiko"` /
`"asyncssh"` / `"auto"`, default `"auto"`) selects the `sftp:` backend;
precedence is an explicit class attribute > this env var > auto-detect
(prefers asyncssh if importable). `gs:` honors `STORAGE_EMULATOR_HOST` (set
into `os.environ` for the `google-cloud-storage` client, e.g. for a local
emulator) when configured on the path/backend.

## Testing helpers (`pathlib_next.testing`)

Not imported by `pathlib_next/__init__.py` (needs `pytest`, a test-only
dependency) — import explicitly: `from pathlib_next.testing import
PathContract`.

- **`PurePathContract`** — pure-path tests (name/suffix/stem, parent/
  parents, joinpath/`/`, match). Requires only a `root` fixture.
- **`ReadPathContract(PurePathContract)`** — read-only I/O tests (exists/
  is_dir/is_file, read_text/read_bytes, iterdir, stat). `root` fixture must
  point at a directory pre-populated with the standard fixture tree
  (`a.txt`, `b.py`, `.hidden.txt`, `sub/c.py`, `sub/nested/d.py`,
  `empty_dir/`).
- **`PathContract(ReadPathContract)`** — full read/write contract (mkdir,
  write_text/write_bytes, unlink, rmdir, rm(recursive=True), copy, move,
  touch(exist_ok=False), mkdir(parents=True)). `root` fixture must be
  writable.

Subclass one of these with your own `root` fixture to verify a custom
`Path`/`UriPath` implementation against the shared contract.

## Utilities (`pathlib_next.utils`)

- **`glob.glob(path, *, dironly=False, root_dir=None, recursive=False,
  include_hidden=False, case_sensitive=None) -> Iterable[path-like]`** — the
  engine behind `Path.glob()`/`rglob()`; works over anything exposing
  `iterdir()`/`is_dir()`/`name`/`parents`/`has_glob_pattern()`. Dotfiles are
  excluded from `*`/`?` matches unless `include_hidden=True`.
  **`glob.full_match(segments, pattern, case_sensitive) -> bool`** —
  pathlib 3.13 `full_match()` semantics, `"**"` matches zero or more
  segments. **`glob.RECURSIVE`** = `"**"`.
- **`sync.PathSyncer(checksum=None, /, remove_missing=False,
  follow_symlinks=True, hook=None, ignore_error=False)`** — one-way
  checksum-driven tree sync between any two `Path` implementations.
  `checksum` defaults to `utils.checksum.md5`. `.sync(source, target, /,
  dry_run=False, ignore_error=False)` copies/creates in `target` whatever
  differs from `source`; `remove_missing=True` also removes `target`
  entries absent from `source`. `hook`/`.log()`/subclassing `.log()` are the
  progress/logging seams; `SyncEvent` enum names the events fired.
  **`sync.PathAndStat`** — a `Path` + cached `stat()` (`None` if missing);
  `is_*` attribute access delegates to the cached stat, returning a
  false-returning callable when the path doesn't exist.
- **`stat.FileStat(FileStatLike)`** — `FileStat(st_mode=None, st_size=0,
  st_mtime=0, is_dir=False)`, slotted, for backends without a real
  `os.stat_result` (`MemPath`, `HttpPath`, ...). `FileStat.from_stat(stat)`
  copies recognized fields from any stat-like object (passes an existing
  `FileStat` through unchanged). `FileStat.from_path(path, *,
  follow_symlink=True) -> FileStat | None` (`None` on `FileNotFoundError`).
  `is_dir()`/`is_file()`/etc. are **methods**, not properties — `if
  st.is_dir` (no parens) is always truthy.
- **`checksum.md5(path, chunk_size=65536) -> str`** /
  **`checksum.sha256(path, chunk_size=65536) -> str`** — streaming file
  checksums over any `Path`.
- **`archive.make_archive(src, format, target)`** (`format` is `"zip"` or
  `"tar"`) / **`archive.unpack_archive(archive, dest)`** (format
  auto-detected from `archive.name`, falling back to magic-byte sniffing) —
  stream-first, so `src`/`target`/`archive`/`dest` can be any `Path`
  implementation, not just local files.
- **`LRU(func, maxsize=128)`** — thread-safe memoizing cache wrapping
  `func`, itself callable; `.invalidate(*args)` evicts and recomputes one
  entry; `.maxsize` is a settable property that evicts down to the new size.
- **`notimplemented(method)`** — decorator marking a protocol method;
  raises `NotImplementedError` naming the method when called. Callers that
  want a graceful fallback catch `NotImplementedError` (e.g. `move()` falls
  back to copy+unlink when `rename` isn't implemented).
- **`sizeof_fmt(num) -> str`** — human-readable byte size (`"1.5K"`, ...).
  **`parsedate(date) -> float`** — epoch seconds from a `str`/
  `time.struct_time`/`tuple`/`float`; unparseable or `None` input returns
  `0`, not "now". **`get_machine_ips() -> list[IPv4Address | IPv6Address]`**
  — `lru_cache(maxsize=1)`d.
