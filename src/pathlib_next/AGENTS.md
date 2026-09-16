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
| `uri` | `uritools`, `netimps>=0.2.0` | `pathlib_next.uri` and **every** URI scheme (`file:`, `data:`, `ftp(s):`, archives included) |
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

## Pure-path / I/O base (`pathlib_next.path`)

- **`Pathname`** — ABC for a pure (no I/O) path. Abstract: `segments`,
  `parts`, `parent`, `with_segments(*segments)`, `as_uri()`,
  `relative_to(other)`. Derived: `name`, `suffix`, `suffixes`, `stem`
  (suffix rules of the running interpreter), `with_name`/`with_stem`/
  `with_suffix` (`ValueError` for `""`, `.` or a separator), `parents`,
  `is_relative_to(other)`, `joinpath(*args)`, `/` and `"prefix" / path`,
  `root`/`drive`/`anchor` (`root` is `"/"` when the first segment is empty;
  `drive` is `""`), `match(path_pattern, *, case_sensitive=None)` (pathlib's
  right-anchored per-segment match; empty pattern → `ValueError`),
  `full_match(pattern, *, case_sensitive=None)` (3.13 semantics),
  `as_posix()`, `has_glob_pattern()`. `is_absolute()` is a stub raising
  `NotImplementedError` unless a subclass overrides it.
  - `__eq__`/`__hash__` default to `(type(self), tuple(self.segments))`: exact
    type, so a subclass never equals its base. `LocalPath`/`PosixPathname`/
    `WindowsPathname` keep `pathlib.PurePath` equality; `Uri` compares its URI
    text. Override both together.
  - A `str` argument to `is_relative_to()` is parsed standalone via
    `self.with_segments(other)`, which keeps per-instance state (a `MemPath`
    backend). Normalize strings the same way in subclasses: `type(self)(x)`
    drops that state.
- **`Path(Pathname, Chmod, Stat, BinaryOpen)`** — base class for I/O paths.
  `Path(*args)` on the bare class constructs a `LocalPath`.
  - Operation precedence: `Path.__init_subclass__` re-asserts pathlib_next's
    `copy`, `move`, `exists`, `rglob`, `read_text`, `write_text` and
    `symlink_to` on any subclass that would otherwise inherit stdlib
    `pathlib`'s (and `stat`/`chmod`/`glob`/`walk`/`_scandir` for a class mixing
    a concrete stdlib path without `LocalPath`). A method defined in the
    subclass itself always wins.
  - `is_hidden()` — name starts with `"."`. `__iter__()` is `iterdir()`.
  - `samefile(other_path)` — compares `(st_dev, st_ino)`; `NotImplementedError`
    when `stat()` lacks them (`LocalPath` uses pathlib's).
  - `iterdir() -> Iterator[Self]` — **stub** (`NotImplementedError`); a
    listable `Path` must implement it (or, on `UriPath`, `_listdir()`/
    `_scandir()`).
  - `_scandir() -> Iterator[tuple[str, FileStat | None]]` — non-following stat
    per entry; default: `iterdir()` + one `stat(follow_symlinks=False)` per
    child. Consumed by `walk()`, `glob()`, `rm(recursive=True)` and
    `PathSyncer`; override it when the listing call already returns metadata.
    `None` means "unknown", never "missing".
  - `glob(pattern, *, case_sensitive=None, include_hidden=True,
    recursive=None, dironly=None, recurse_symlinks=False, native=True)` —
    pathlib semantics: hidden entries included, `**` never descends into
    directory symlinks (`recurse_symlinks=True` → `NotImplementedError`), a
    trailing `**` selects files too on 3.13+, a missing or non-directory base
    yields nothing, `""` → `ValueError`, an absolute pattern →
    `glob.NonRelativePatternError`. `recursive=None` enables recursion when a
    component is `**`; an explicit value wins. Validates eagerly, selects
    lazily. On a remote scheme a recursive glob lists every directory of the
    subtree.
    - **`pattern=None`** expands the pattern THIS PATH CARRIES
      (`LocalPath("/etc/*.conf").glob(None)`), splitting at the first
      wildcard — the supported form for a path that is itself a pattern.
      `""` still raises.
    - **`native=True`** (default) follows the running interpreter on the two
      rules pathlib changed mid-series: a trailing `/` is ignored before 3.11
      and selects directories only from 3.11; `a**` raises `ValueError`
      before 3.13 and is a plain wildcard from 3.13. `native=False` applies
      one rule on every version (trailing `/` → directories only, `a**` → a
      plain wildcard), so a pattern answers the same on every interpreter and
      backend; `pathlib_next.testing`'s contract suite uses it.
  - `rglob(pattern, **same_kwargs)` — `glob(f"**/{pattern}", recursive=True)`.
    `pattern=None` is `glob(None)`.
  - `walk(top_down=True, on_error=None, follow_symlinks=False)` — drives
    `_scandir()`; its stats are trusted only with `follow_symlinks=False`. No
    symlink-cycle protection when following (only `LocalPath` has pathlib's).
  - `touch(mode=None, exist_ok=True)` — `FileExistsError` when `exist_ok=False`
    and the path exists; never truncates; creates with `open("x")` (falls
    back to `"w"` when `x` is unsupported); `chmod(mode)` only when `mode` is
    passed (unmasked); does not update an existing file's mtime.
    `LocalPath`/`FileUri` use pathlib's `touch()`.
  - `_mkdir(mode)` (stub) / `mkdir(mode=0o777, parents=False, exist_ok=False)`.
  - `unlink(missing_ok=False)`, `rmdir()` — stubs.
  - `rm(recursive=False, missing_ok=False, ignore_error=False)` — extension.
    `ignore_error` is a bool or `callable(error, path) -> bool` (True
    swallows); each error is offered once. Recursive removal is bottom-up and
    never descends through a directory symlink or a Windows junction (the
    link itself is removed).
  - `rename(target)` — stub. Implementations return the new path.
  - `_symlink_to(target, target_is_directory=False)` (stub; receives a path
    object) / `symlink_to(target, target_is_directory=False, *, force=False)`
    — `force=True` unlinks an existing non-directory entry first (not atomic;
    never removes a directory). A `str` target is normalized by the
    overridable `_symlink_target()` and stored verbatim; relative stays
    relative. Implemented by `LocalPath` and `SftpPath` only.
  - `copy(target, *, overwrite=False, follow_symlinks=True,
    preserve_metadata=True, recursive=False, ignore_error=None,
    progress=None) -> None`
    - Existing target: `FileExistsError` unless `overwrite=True`; a directory
      target of a file copy → `IsADirectoryError`; a directory source needs
      `recursive=True`; copying into its own subtree → `OSError(EINVAL)`; onto
      the same file (or a case-insensitive alias) → `OSError(EINVAL)`.
    - The source is opened before the target is touched; a failed stream
      removes the partial target.
    - A recursive copy refuses any child name that would not stay inside
      `target` — `..`, and `\`/`:`/a trailing dot when the target reads
      names with Windows rules (`utils.is_windows_flavoured()`). The names
      come from a listing the destination does not control (an archive, a
      remote index, an object-store key), and on a Windows target `"C:x"`
      joins to a drive-relative path outside it. Raised as `ValueError`
      through `ignore_error`, per child, like any other child failure.
    - `follow_symlinks=False` on a symlink recreates the link
      (`NotImplementedError` if either side cannot).
    - `preserve_metadata=True` copies permission bits only, and only a mode the
      source backend really reported (`FileStat.mode_known`).
    - `ignore_error`: `True` suppresses child errors of a recursive copy,
      `False`/`None` raise; a callable is called as `ignore_error(error)` and
      the error is **always** suppressed (its return value is ignored — not
      `rm()`'s `(error, path)` predicate).
    - `progress(path, bytes_copied, total_size | None)` per chunk of each file
      streamed; not called by `SftpPath`'s asyncssh recursive fan-out.
    - A `str` target goes through `_coerce_target()`: `with_segments()` by
      default, a URI parse on `UriPath` (so `copy("s3://b/k")` crosses
      schemes).
  - `move(target, *, overwrite=False)` — validates first (missing source →
    `FileNotFoundError`, file onto directory → `IsADirectoryError`, existing
    target without `overwrite` → `FileExistsError`; a same-file spelling is
    renamed in place). Tries `rename()` when `_rename_compatible(target)`,
    falling back to `copy(recursive=True)` + `rm`/`unlink` on
    `NotImplementedError` or `OSError(EXDEV)`. `overwrite=True` replaces a
    local file atomically (`replace()`); elsewhere the target is unlinked just
    before the rename. Returns `rename()`'s result (`None` on the fallback).
- **`FsPathLike`** — `Protocol` with `__fspath__() -> str`.
  **`PathLike`** = `str | Path`; **`PurePathLike`** = `str | Pathname`.

## Local filesystem (`pathlib_next.fspath`)

- **`LocalPath`** — `pathlib.WindowsPath`/`PosixPath` (by `os.name`) with
  `Path` mixed in; stdlib behavior except where overridden: `_scandir()`
  (tuples from `os.scandir` lstat), `walk()`, `glob()`, `copy()`, `move()`,
  `stat()`/`chmod()` (`follow_symlinks=` on 3.9; `chmod` accepts octal
  strings), `is_dir()`/`is_file()` (`follow_symlinks=` before 3.13),
  `_symlink_to()`, `_chown()` (`shutil.chown`; `NotImplementedError` where
  `os.chown` is missing, i.e. Windows), plus pathlib_next's `exists`,
  `rglob`, `read_text`, `write_text`, `symlink_to`. `exists()`/`is_*()`
  return `False` for any `OSError`/`ValueError` on every Python version. A
  stdlib `pathlib.Path` is not a `pathlib_next.Path`, and `MemPath`/`Uri`/
  `UriPath` are not stdlib paths.
- **`PosixPathname`** / **`WindowsPathname`** — pure classes over
  `PurePosixPath`/`PureWindowsPath` implementing `Pathname`.

## In-memory filesystem (`pathlib_next.mempath`)

- **`MemPath(*segments, backend=None)`** — `Path` over nested dicts; the
  reference `Path` subclass. Segments may be `str`, `Pathname` or `MemPath`
  (a `MemPath` argument shares its backend; another `Path` →
  `NotImplementedError`). Joined and normalized like `PurePosixPath`.
  - `backend` (a `MemPathBackend`); `parts` is `(segments, backend)`;
    `as_uri()` → `mempath:<quoted posix path>` (no `mempath:` scheme is
    registered; build `MemPath` directly).
  - `stat()` → `FileStat` with `st_size` and `st_mtime` (time of the last
    write); the mode is a placeholder (`mode_known=False`).
  - `open()` supports `r`, `w`, `x`, `a` (binary or text); `+` modes →
    `NotImplementedError`. Writes are visible after `flush()`/`close()`.
  - Not implemented (`NotImplementedError`): `relative_to()`,
    `is_absolute()`, `rename()` (`move()` copies), `chmod()`, `symlink_to()`.
  - A `str` destination to `copy()`/`move()` stays on the same backend.
- **`MemPathBackend(dict)`** — storage: `dict` value = directory,
  `bytearray` (`MemFile`, carrying `mtime`) = file. Pass one instance as
  `backend=` to share a tree; each root `MemPath()` otherwise gets its own.

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
  3986 reference resolution and `..` is not resolved during a join. An
  absolute local path becomes `file:`; a relative one joins like a
  `PurePath`.
  - `/` and `joinpath()` take a `str` as an **already-decoded path**: `?`,
    `#`, `%` and a leading `C:` are ordinary filename characters
    (`base / "cache?v=2"` names that file), which is what `iterdir()` builds.
    A `Uri`/`UriPath` argument keeps URI semantics and is the only form that
    can cross to another endpoint -- where a credential-bearing backend is
    dropped. Dot segments are removed from the joined result either way.
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
    `None`), created on first use and inherited by derived paths.
    `with_backend(backend)` returns a copy using `backend`. A backend is only
    shared within one endpoint (scheme, userinfo, host, port): a join,
    `with_source()` or `UriPath(base, url)` onto another endpoint builds a
    fresh one, so credentials and sessions never follow.
  - Listing: implement `_listdir() -> Iterator[str]` or override
    `_scandir()`; `iterdir()` wraps each name with the entry's stat as a
    single-use hint (the child's first `stat()` returns it, later calls
    re-fetch).
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
  only and returns a `FileUri`. No `symlink_to()`/`readlink()`.
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
    buffer and send `write_method` on close (`"x"` checks then writes, not
    atomic). `"a"`: `append_mode="rewrite"` (GET + full re-upload, not atomic)
    or `"patch"` (`PATCH` with `Content-Range` from `stat()`; a refusal raises
    `PermissionError` for 401/403/405/501, `OSError(EIO)` for other statuses).
  - `unlink()` sends `DELETE` and refuses a directory (`IsADirectoryError`,
    judged by `stat()`); `rmdir()` requires an empty directory. No `mkdir()`,
    `rename()`, `chmod()`.
  - Status mapping: 404/410 → `FileNotFoundError`, 401/403/405/501 →
    `PermissionError`, 409 → `FileExistsError` (`FileNotFoundError` for
    writes), other → `OSError(EIO)` with the status.
- **`DavPath(HttpPath)`** (`dav:`/`davs:`, sent as `http:`/`https:`; `http`
  extra; `schemes.dav`) — same backend and `with_session()`. `stat()`/
  listing via `PROPFIND`. `open("r")` on a collection → `IsADirectoryError`;
  `"w"`/`"x"` `PUT` on close; `"a"` unsupported. `mkdir()` = `MKCOL`
  (missing parent → `FileNotFoundError`). `unlink()` refuses a collection;
  `rmdir()` checks emptiness first; `rm(recursive=True)` is one recursive
  `DELETE` (failed members of a 207 raise). `rename()` = `MOVE` with
  `Overwrite: F` (existing target → `FileExistsError`), no credentials in
  `Destination`. 423 → `PermissionError`. No `chmod()`.
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
    `perm` fact); servers without it fall back to `NLST`/`SIZE`.
  - Reads download the whole file into memory; `"w"`/`"x"`/`"a"` (`APPE`)/
    `"r+"` buffer in memory and upload on close (`"x"` checks then writes).
  - `rename()` on the same server. `chmod()` via `SITE CHMOD`
    (`NotImplementedError` when the server lacks it or
    `follow_symlinks=False`).
- **`SftpPath`** (`sftp:`; `sftp` or `sftp-async` extra; `schemes.sftp`)
  - `SftpPath(*uris, backend=None, ssh_config=<default>)` — `ssh_config`:
    default `~/.ssh/config`, `None` for none, a path or iterable of paths;
    inherited by derived paths.
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
    `NotImplementedError` unless `connect_opts["sock"]` is given. Connections
    cached per (backend, source, thread), replaced when dropped.
  - **`AsyncsshSftpBackend(connect_opts=None, *, max_concurrency=None,
    sftp_version=4, ssh_config=<default>, timeout=60.0)`**. asyncssh verifies
    host keys against `known_hosts`/ssh_config; opt-out
    `connect_opts={"known_hosts": None}`. `timeout` bounds single requests
    (a timed-out request is cancelled and raises `TimeoutError`); recursive
    `copy()`/`rm()` and streamed reads/writes are unbounded (use asyncssh's
    `connect_timeout`/`keepalive_interval`). One connection per (backend,
    source), served by one shared background event loop thread; not
    fork-safe (rebuilt after `fork()`). A sync `Path` call made on that loop
    thread (inside a callback running there) raises `RuntimeError`.
    `max_concurrency` (`None` → `DEFAULT_MAX_CONCURRENCY = 16`) bounds
    requests in flight and files open during recursive `copy()` (target on
    the same host only) and `rm()`.
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
    `supported_checksums()` (`NativeChecksum`): paramiko probes the
    `check-file-handle` extension (OpenSSH lacks it); asyncssh never has it;
    every failure is `NotImplementedError`. `__fspath__()`/`host_fspath()`
    return `.path`.
- **`S3Path`** (`s3://bucket/key`; `s3` extra; `schemes.s3`) — `bucket`,
  `key` (one trailing `/` dropped: `s3://b/dir/` is `dir`).
  `S3Backend(**client_kwargs)` → one lazily built, thread-shared
  `boto3.client("s3", **client_kwargs)` (default: boto3's own credential and
  endpoint configuration); `BaseS3Backend.client()` is the override point.
  - Directories are key prefixes: `mkdir()` writes a zero-byte `key/` marker,
    `rmdir()` needs an empty prefix, a key that is both an object and a prefix
    is the object (in `stat()` and listings). No hierarchy enforcement (writes
    below a missing "directory" succeed).
  - Reads stream; `"w"`/`"x"`/`"r+"` spool and upload on close; `"x"` is a
    conditional put (check-then-put above 5 GiB); `"a"` unsupported.
    `st_mtime` from `LastModified`.
  - `rename()`: server-side copy + delete in the same bucket; a prefix
    directory → `NotImplementedError` (`move()` copies). `rm(recursive=True)`
    batch-deletes; at the bucket root → `PermissionError`. No `chmod()`.
- **`GsPath`** (`gs://bucket/key`; `gs` extra; `schemes.gs`) — `bucket_name`,
  `key`. `GsBackend(**client_kwargs)` → `google.cloud.storage.Client(
  **client_kwargs)` unchanged (emulator: `client_options={"api_endpoint":
  url}, use_auth_w_custom_endpoint=False`, or set `STORAGE_EMULATOR_HOST`
  yourself); `BaseGsBackend.client()`. Same prefix model and rename rules as
  `S3Path` (same bucket); reads load the whole object; `"x"` uses
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
  prefix model as `S3Path`; `rename()` within one container; `"x"` sends
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
  - Writes: zip only, and only with a local `file:` outer (else
    `NotImplementedError`). `"w"`/`"x"`/`"r+"`, `mkdir()`, `unlink()`,
    `rmdir()`, `rename()` (same archive; replaces like POSIX `rename`);
    parents must exist; `"a"` unsupported. Every mutation replaces the archive
    atomically (temp file + `os.replace`) and keeps other members' metadata,
    the comment and any prefix bytes. A write uses the normalized name; a
    name that escapes the root fails and creates nothing.
    `tar:` (plain, gz, bz2, xz) is read-only.

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
  exclusive modes, `unlink()`, `rmdir()`, `rm()`, `copy()` (recursive),
  `move()`, `rename()`, `touch()`. Capability attributes: `supports_rename`,
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
  names need `include_hidden=True`. `glob.parse_pattern(pattern) ->
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
  - `ignore_error`: bool or `callable(error, source, target, event) -> bool`,
    offered once per error; a tolerated error is logged at WARNING on logger
    `pathlib_next.sync` and reported to `hook` as `SyncEvent.Error`.
    `.log(msg, *args)` (INFO on the same logger) is overridable.
  - Safety, all through `ignore_error`: a missing root `source` →
    `FileNotFoundError`; overlapping `source`/`target` (same implementation
    and backend) → `ValueError`; a child name that would leave `target`
    (`..`, or `\`/`:` on a Windows target) → `ValueError`; a symlink inside
    `target` is replaced, never followed. Listing entries with unknown stats
    are re-stat'd.
  - `remove_missing=True` deletes target entries absent from the source. With
    `False`, a non-empty target directory whose source became a file or link
    is kept (`IsADirectoryError`, event `TypeMismatch`).
  - `follow_symlinks=False` + `symlink_mode="preserve"` recreates source links
    with the raw `readlink()` text (target must implement `symlink_to()`, else
    `NotImplementedError` through `ignore_error`); `"reject"` raises
    `NotImplementedError`.
  - Changed files are written to a hidden temporary sibling and renamed over
    the target where the target supports `rename()`. FIFOs, sockets and
    devices are skipped. A dry run makes the same decisions without changes.
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
  (`S_IFREG|0o444` / `S_IFDIR|0o555`) with `mode_known=False`.
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
  would leave `dest` are skipped; tar hard links and links to regular files
  are extracted as copies, other links skipped with `UserWarning`). Also
  importable from `pathlib_next.utils`.
- **`is_safe_child_name(name, *, windows=False) -> bool`** — `False` for a
  non-`str`, `""`, `.`, `..`, or a name containing `/` or NUL; with
  `windows=True` also `\`, `:`, and names that are empty or dot-only after
  trailing dots/spaces are stripped. **`is_windows_flavoured(path) -> bool`**
  — `True` for a `PureWindowsPath` or a path whose `filepath` is one.
- **`LRU(func, maxsize=128, on_evict=None)`** — thread-safe memoizing cache,
  called like `func`. `on_evict(key_tuple, value)` runs for every dropped
  value (overflow, `maxsize` shrink, `invalidate`/`discard`, a losing
  concurrent miss), outside the lock, exceptions suppressed. `discard(*args)
  -> bool`, `invalidate(*args)` (discard + recompute), settable `maxsize`.
- **`as_mode(mode) -> int`** — `int` passes through; `str` is octal (optional
  `0o`), any non-octal digit → `ValueError`.
- **`as_owner(uid, gid) -> (int | None, int | None)`** — `-1` → `None`;
  `str` names pass through. **`UNCHANGED = None`**.
- **`as_error_handler(ignore_error, *, default=False) -> callable`** — a
  callable passes through untouched (arity is the call site's: `rm` `(error,
  path)`, `copy` `(error)`, `PathSyncer` `(error, source, target, event)`);
  a bool (or `None` → `default`) becomes a constant-returning callable.
- **`notimplemented(method)`** — decorator; calling raises
  `NotImplementedError("Method not implemented: <name>")`.
- **`sizeof_fmt(num) -> str`** — `1536` → `"1.5K"`.
- **`parsedate(date) -> float | int`** — UTC epoch seconds from an HTTP date
  string (zone offset applied, none = UTC), a `struct_time`/tuple (UTC minus
  any offset), or a number (returned unchanged); `None` or unparseable → `0`.
