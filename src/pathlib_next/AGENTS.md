# `pathlib_next` — public API header

Header-file-style reference for the `pathlib_next` package: every public
export with its signature, arguments, contract, and gotchas, so this module
can be consumed without reading its source. Kept current with the public
API. For the project overview, install extras, and code layout, see the
<https://github.com/jose-pr/pathlib-next>. Any behavioral divergence from `pathlib.Path` is
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
  - `_symlink_to(target, target_is_directory=False)` (not implemented by
    default) / `symlink_to(target, target_is_directory=False, *,
    force=False)` — same primitive/wrapper split as `_mkdir`/`mkdir`: a
    backend implements only `_symlink_to()` and receives an already
    normalized path object (a `str` target is turned into one by the
    wrapper, as `copy()`/`move()` do), then reads the raw target string the
    way its transport needs (`Uri.path` on the wire, `os.fspath()`
    locally). `force=` is this library's extension: `False` is
    stdlib-exact, `True` unlinks an existing **non-directory** entry at the
    link path first (never a directory) and is **not** atomic. Listed in
    `_OPERATION_NAMES`, since no stdlib version accepts `force=`.
  - `copy(target, *, overwrite=False, follow_symlinks=True,
    preserve_metadata=True, recursive=False, ignore_error=None,
    progress=None)` — `follow_symlinks`/`preserve_metadata` names match
    CPython 3.14's `Path.copy()`; `overwrite` is this library's own
    extension (3.14 always raises if the destination exists).
    `preserve_metadata` defaults `True` here (3.14 defaults `False`) and
    only preserves `st_mode`, not timestamps/xattrs. `ignore_error`, when
    given, receives exceptions instead of raising (same contract as
    `rm()`'s callable form); `None` (default) fails on the first error.
    `progress`, when given, is called as `progress(path, bytes_copied,
    total_size)` per chunk written for each file streamed (`path` is the
    source file; `total_size` is `None` if unknown); with `recursive=True`
    this fires once per copied file, giving per-file identity alongside
    byte progress. `progress=None` (default) has no per-chunk overhead and
    is bytewise identical to before this kwarg existed. Not honored by
    `SftpPath`'s asyncssh concurrent fan-out (native transfer, out of
    scope) — see `docs/divergences.md`.
  - `move(target, *, overwrite=False)` — tries `rename()` first, falls back
    to `copy(recursive=True)` + `rm(recursive=True)`/`unlink()` when
    `rename()` raises `NotImplementedError`.
- **`PathLike`** — `Union[str, Path]`. **`PurePathLike`** — `Union[str,
  Pathname]`. **`FsPathLike`** — `Protocol` requiring `__fspath__() -> str`.

## Local filesystem (`pathlib_next.fspath`)

- **`LocalPath`** — `pathlib.WindowsPath`/`PosixPath` (by `os.name`) with
  this library's `Path` mixed in via MRO. Behaves exactly like
  `pathlib.Path` for anything not explicitly overridden (see
  `docs/divergences.md`); overrides `_scandir()`, `walk()`, `copy()`,
  `move()`, `stat()`, `chmod()`, and `glob()` to keep this project's
  contracts (tuple-yielding `_scandir`, extended copy/move kwargs,
  `follow_symlinks=` support pre-3.10) regardless of what a given Python
  version's own `pathlib.Path` does at the same MRO position.
  Stdlib inheritance is intentionally local-only: `MemPath`, `Uri`, and
  `UriPath` implement the pathlib_next bases but are not stdlib
  `PurePath`/`Path` instances because stdlib construction and operations
  assume OS path syntax and a local filesystem. Conversely, a plain stdlib
  `pathlib.Path` is not a `pathlib_next.Path`.
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
  errors=None, newline=None)`, `copy(target, *, progress=None,
  chunk_size=shutil.COPY_BUFSIZE)` (streams this object's binary content
  into another `BinaryOpen`; `progress(bytes_copied, total_size)` fires
  per chunk when given — `total_size` from `stat().st_size` if `self` also
  implements `Stat` and it succeeds, else `None`; `progress=None` default
  is unchanged `shutil.copyfileobj` behavior).
- **`checksum.NativeChecksum`** — `Protocol`. `checksum(algorithm="md5") ->
  str` (not implemented by default). Optional, backend-native file digest
  (e.g. `SftpPath` against the OpenSSH `check-file@openssh.com` SFTP
  extension) computed server-side instead of streaming content through
  `open("rb")`. Not mixed into the base `Path`/`Pathname` ABC — a plain
  `Path` has no `.checksum` attribute at all; a subclass opts in by mixing
  this protocol in and implementing the method. MUST raise
  `NotImplementedError` (never return a value) when it can't produce a
  genuine digest under the requested `algorithm` — see
  `docs/divergences.md` for why this is a hard contract, not a style
  choice (the S3-ETag-for-multipart-uploads trap in particular).
  `supported_checksums() -> frozenset[str]` (default: `frozenset()`) is a
  companion *advisory* capability query — never raises, lets a caller pick
  a shared algorithm across two paths (e.g. `source.supported_checksums()
  & target.supported_checksums()`) without probing via trial-and-error.
  Advisory only: `checksum()`'s own `NotImplementedError` remains the
  authoritative per-call contract even if a caller skips this query.
  `SftpPath.supported_checksums()` is a real per-connection probe (not a
  static flag) — paramiko exposes no way to read the server's advertised
  SFTP extension list, so the only reliable signal is an actual attempt,
  cached per connection.

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
  present). `__fspath__()` succeeds for a `file:`-scheme URI pointing at
  this machine, and for any scheme with `_host_filesystem_path = True`
  (currently `sftp:` — returns `.path`, meaningful on that URI's own host,
  not the local one); otherwise raises `NotImplementedError`. `host_fspath()`
  is the unambiguous accessor for "path on the URI's own host" — same
  `_host_filesystem_path` gate, but never falls back to local-path
  semantics. See `docs/divergences.md`.
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
  field is empty/`None`. `as_str(sanitize=True) -> str` composes an
  authority string (`scheme://userinfo@host:port`); `sanitize=True` (the
  default) drops the password from `userinfo`, `sanitize=False` is the
  full, credentialed round trip — same name/kwarg as `Uri.as_uri()`, so
  both classes work the same way. `__str__()` is `as_str(sanitize=True)`;
  `__repr__()` redacts the same way (`NamedTuple`'s default would render
  every field, including the password, verbatim — see
  `docs/divergences.md`). The actual data (`.userinfo`, `parsed_userinfo()`,
  `["userinfo"]`) is unaffected by any of this, only display is sanitized.
  `Source.from_str(source, strict=True) -> Source` (`strict=True` raises
  `ValueError` if `source` carries a path/query/fragment).
  `parsed_userinfo() -> (user, password)`.
  `get_scheme_cls(schemesmap=None) -> type[UriPath]` — resolves (and lazily
  loads) the scheme class. `is_local()` — IP-literal `host` (`str` or
  `_IPAddress`) skips resolution via `netimps.try_parse()`; otherwise
  `netimps.resolve(host, "a")` + `resolve(host, "aaaa")` (default backend
  chain: dnspython, then the OS resolver via `getaddrinfo()` — hosts file,
  NSS, DNS, OS cache — then `nslookup`; `host` is local if ANY resolved
  address is; empty result -> not local, never an exception for a
  genuinely non-resolving name). `netimps.is_local_address()` then decides
  membership per address (real interface enumeration via
  `netimps.get_interfaces()`, not DNS-based guessing). `lru_cache
  (maxsize=256)`d per `Source` value; never call on a hot path uncached.
  Requires `netimps>=0.2.0` (part of the `uri` extra; `resolve()`'s
  OS-resolver-chain support landed in 0.2.0 — earlier versions were
  dnspython-only).
- **`Query(str)`** (`uri.query`) — a URI query string, buildable from a
  `str`, a sequence of `(key, value)` pairs, or a mapping (`value` may be a
  sequence to repeat the key). `Query(query, *, encoding="utf-8",
  separator="&")`. `decode() -> list[tuple[str, str | None]]`,
  `__iter__()` (iterates decoded pairs), `to_dict(*, single=False) ->
  dict[str, list[str | None]]` (or `dict[str, str | None]` when
  `single=True`, last value wins).

Built-in scheme modules live under `uri/schemes/` — see the table in the
<https://github.com/jose-pr/pathlib-next>. `PATHLIB_NEXT_SFTP_BACKEND` env var (`"paramiko"` /
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
  follow_symlinks=True, symlink_mode="preserve", hook=None,
  ignore_error=False, quick_check=True)`** — one-way checksum-driven tree
  sync between any two `Path` implementations. `checksum=None` (the
  default) resolves to a policy that prefers each side's
  `protocols.checksum.NativeChecksum.checksum()` (no network transfer
  needed just to decide whether a copy is needed) over streaming, but only
  trusts a native digest from one side if the OTHER side can also produce a
  digest under the same algorithm (native or streamed) — otherwise BOTH
  sides fall back to streaming (`utils.checksum.md5`/`stream`), never a
  native-vs-streamed comparison under a mismatched algorithm. A
  caller-supplied `checksum` callable disables this entirely and is invoked
  exactly as before (once per side, compared with `==`). `quick_check=True`
  (default) adds a metadata-only pre-check (`st_size` + `st_mtime`, already
  cached, no extra round trip) for any pair where at least one side is
  non-local (`Uri.is_local()`/DNS-lookup-failure-safe; a side without
  `is_local()` at all is treated as local) — both matching skips the
  checksum call entirely; either differing always falls through to a real
  checksum (never concludes "changed" from metadata alone). Local-to-local
  pairs never engage this pre-check regardless of the flag's value.
  `quick_check=False` disables the pre-check entirely.
  `.sync(source, target, /, dry_run=False, ignore_error=False)`
  copies/creates in `target` whatever differs from `source`;
  `remove_missing=True` also removes `target` entries absent from `source`.
  `follow_symlinks=True` (default) resolves through a symlink source during
  traversal exactly like content sync (unchanged). With
  `follow_symlinks=False`, a symlink source is reported as such and
  `symlink_mode` decides what happens: `"preserve"` (default) creates a
  matching symlink on `target` using the exact raw, unresolved target
  string `readlink()` returned — dangling links and relative targets
  included, never validated or resolved against `source`'s parent;
  `"reject"` raises `NotImplementedError` instead (the sole behavior before
  this kwarg existed). If `target`'s implementation has no `symlink_to()`
  at all (every backend except `LocalPath` and `SftpPath` — see
  `docs/divergences.md`), `"preserve"` mode also raises
  `NotImplementedError`, through the same `ignore_error`/`hook()` flow as
  every other branch, not a silent skip. `hook`/`.log()`/subclassing
  `.log()` are the progress/logging seams; `SyncEvent` enum names the
  events fired (`SyncEvent.Symlink` covers symlink creation, replacement,
  and the not-implemented/error path alike).
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
  checksums over any `Path`. **`checksum.stream(path, algorithm="md5",
  chunk_size=65536) -> str`** — the generic (runtime `algorithm`) form of
  the above, used by `PathSyncer`'s streaming fallback. **`checksum.native(
  path, algorithm="md5") -> str | None`** — tries `path.checksum(algorithm)`
  (`protocols.checksum.NativeChecksum`); returns `None` (never raises) if
  `path` doesn't implement the protocol at all, or raises
  `NotImplementedError` for `algorithm`.
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
