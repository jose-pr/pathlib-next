# Divergences from `pathlib`

`pathlib_next` targets `pathlib.Path` parity: same method names, signatures,
semantics and exception types wherever a `pathlib.Path` equivalent exists.
Extensions (extra optional kwargs, new methods like `rm()`/`sync`) are
allowed. Any *behavioral* divergence from `pathlib` on a method that exists
in both must be listed here -- no silent divergence.

`LocalPath` is `pathlib.WindowsPath`/`pathlib.PosixPath` with our `Path` mixed
in via MRO, so unless noted otherwise it behaves exactly like `pathlib.Path`
(it inherits the real implementation for anything not explicitly overridden).
The divergences below apply to `Uri`/`UriPath` and `MemPath`.

## Type relationships

Stdlib inheritance is deliberately limited to local filesystem paths:

- `LocalPath` subclasses both `pathlib.Path` and `pathlib_next.Path`.
- `PosixPathname` and `WindowsPathname` subclass the matching stdlib
  `PurePath` classes and `pathlib_next.Pathname`.
- `MemPath`, `Uri`, `UriPath`, and custom virtual or remote implementations
  subclass the generic pathlib_next bases, not `pathlib.Path`/`PurePath`.
- A plain stdlib `pathlib.Path` is not a `pathlib_next.Path`.

The generic classes cannot safely inherit the stdlib classes: pathlib parses
OS-specific path syntax and supplies operations whose semantics assume a local
filesystem, neither of which applies to a URI, archive member, object-store key,
or in-memory path. Registering stdlib paths as virtual `pathlib_next.Path`
subclasses would likewise promise pathlib_next's extended operation contract on
Python versions where stdlib paths do not implement it. Code accepting every
implementation should type against `pathlib_next.Path` or its documented
protocols; code requiring an OS path should type against `pathlib.Path`.

### Operation precedence in `Path` subclasses

Because concrete classes mix a `pathlib` class with `pathlib_next.Path`, the
MRO alone would decide which library implements a given method -- and *which
one wins changes with the interpreter version*, since stdlib `pathlib` keeps
gaining and changing methods. That produced version-dependent behavior in
both directions: CPython 3.14's new `copy()`/`move()` displaced ours (loudly
on non-local backends, silently and with different timestamp semantics on
local ones), while pre-3.12/3.13 stdlib lacked keywords our protocols
promise (`exists(follow_symlinks=)`, `read_text`/`write_text`'s `newline=`).

`pathlib_next.Path.__init_subclass__` therefore re-asserts the pathlib_next
implementation of `copy`, `move`, `exists`, `rglob`, `read_text` and
`write_text` for any subclass that would otherwise inherit stdlib's. This
applies automatically to downstream classes built with the documented
composition pattern (`class X(PosixPathname, Path)`), so implementers do not
have to hand-write forwarding methods.

Only stdlib `pathlib` is displaced: a subclass or mixin that defines one of
these operations itself always keeps its own implementation.

| Method | pathlib behavior | Our behavior | Why |
| --- | --- | --- | --- |
| `Uri("a").parent` | `PurePosixPath("a").parent == PurePosixPath(".")` | `Uri("a").parent` has path `""` (`Uri("")`, which round-trips) | `Uri` has no cwd-relative concept of `"."` -- an empty path is the URI-natural "no path" representation. Changing this would make `Uri("")` non-idempotent under `.parent`. |
| `with_name()` / `with_suffix()` / `with_stem()` on `Uri`/`UriPath` | N/A (pathlib has no query/fragment) | Preserve the URI's query and fragment (implemented via `with_path`, which carries them over) | Deliberate extension: `UriPath("http://h/a?x=1").with_suffix(".txt")` keeping `?x=1` matches how most callers actually want to retarget just the path component of a URL. **User decision, 2026-07-11.** |
| `Path.__iter__` | `pathlib.Path` is not iterable (no `__iter__`) | `iter(path)` is `path.iterdir()` | Deliberate extension for ergonomic `for child in path:` loops. **Caution:** on remote schemes (http/sftp) this is a network call. **User decision, 2026-07-11.** |
| `Path.copy(target, ...)` | CPython 3.14 `Path.copy(target, *, follow_symlinks=True, dirs_exist_ok=False, preserve_metadata=False)`; always raises if `target` exists | Ours predates 3.14. Signature: `copy(target, *, overwrite=False, follow_symlinks=True, preserve_metadata=True)`. `overwrite=True` unlinks an existing non-directory target first; `preserve_metadata` defaults to **True** (opposite of 3.14) and only propagates `st_mode`, not timestamps/xattrs | Argument names aligned with 3.14 where cheap; `preserve_metadata=True` default kept for backward compat with this method's pre-existing (pre-3.14-alignment) behavior of always copying the mode bits. Full metadata preservation (timestamps, xattrs) is not implemented. |
| `Path.move(target, ...)` | Not in `pathlib` at all | Our own extension: tries `rename()`, falls back to copy+unlink | N/A -- pure extension, no pathlib method to diverge from. |
| `Path.rm(recursive=, missing_ok=, ignore_error=)` | Not in `pathlib` (closest: `shutil.rmtree`) | Our own extension. Recursive removal deletes bottom-up and uses non-following stat/listing metadata, so a symlink to a directory is unlinked rather than traversed. | N/A -- pure extension. Non-following recursive deletion avoids deleting through symlinked directory targets and lets backends with metadata-rich listings remove trees without a stat round trip per child. |
| `Path.symlink_to(target, target_is_directory=False, *, force=False)` | `pathlib.Path.symlink_to(target, target_is_directory=False)` -- raises `FileExistsError` if anything already exists at the link path | Adds a keyword-only `force=`. `force=False` (the default) is stdlib-exact. `force=True` unlinks an existing **non-directory** entry at the link path first, then creates the symlink; an existing *directory* is never removed and the underlying error propagates. Not atomic: no filesystem or transport offers "replace a symlink" as one operation, so the path briefly does not exist between the unlink and the symlink. | Additive extension (an extra optional kwarg, per the parity contract). No backend can offer this atomically, so every consumer was re-implementing the same unlink-then-symlink dance -- it is path semantics, not transport semantics, so it belongs at the `Path` layer where one implementation serves every backend. Backends implement only the `_symlink_to()` primitive (same `_mkdir`/`_open` shape) and get `force=` for free. Because no stdlib version accepts the keyword, `symlink_to` is in `_OPERATION_NAMES` so `LocalPath` honors it too. |
| `Path.chown(uid=None, gid=None, *, follow_symlinks=True)` | Not in `pathlib` at all -- it has `owner()`/`group()` **readers** but no writer (the stdlib writers are `os.chown`/`shutil.chown`, which are functions over a path, not path methods) | Our own extension. `None` (default) leaves a field unchanged; `-1` is accepted as an alias for `None` (`os.chown`'s own sentinel); an `int` is a uid/gid and a `str` is a user/group name. A call where both fields are unchanged short-circuits without touching the backend. Implemented for `LocalPath`/`FileUri` (via `shutil.chown`, or `os.lchown` for `follow_symlinks=False`) and `SftpPath` (`setstat`); `NotImplementedError` elsewhere. | Ownership was the one permission attribute `stat()` could report (`st_uid`/`st_gid`) that nothing could write back. The valuable part is centralizing the **"unchanged" sentinel** on `Path` (`utils.as_owner()`): `os.chown` spells it `-1`, SFTP omits the field, other middlewares use `None` -- normalizing per-scheme would be three chances to disagree. Backends implement `_chown()` and receive an already-canonical pair. SFTPv3 sends uid/gid as one paired attribute, so `SftpPath` reads the current owner for whichever field is unchanged rather than guessing a value. |
| `Path.chmod(mode, ...)` accepting a `str` | `mode` must be an `int`; a `str` raises `TypeError` | Additionally accepts a `str`, parsed as **octal**: `"0755"`, `"755"` and `0o755` all mean the same thing. An optional `0o` prefix is allowed; any character outside `[0-7]` raises `ValueError` rather than being coerced. | The string form is how modes are written in `chmod(1)`, Ansible, Dockerfiles and shell scripts, so config-driven callers arrive holding one. Accepted only with an **explicit base 8** (`utils.as_mode()`), never a plain `int()`: `int("0755")` in decimal is 755 == `0o1363`, a different *and valid* mode, so a fallback to decimal would set plausible-but-wrong permissions with nothing raising -- which is exactly why stdlib refuses strings. Parsing in one shared helper is what makes the base non-negotiable across the five backends that implement `chmod` directly. |
| `PathSyncer` / `Query` / `Source` | N/A | Our own extensions | N/A -- pure extensions, no pathlib equivalent. |
| `PathSyncer(follow_symlinks=False).sync()` on a symlink source | N/A (no pathlib equivalent) | Previously always raised `NotImplementedError`. Now controlled by the new `symlink_mode` constructor kwarg (`"preserve"` default, `"reject"` opt-out): `"preserve"` creates a matching symlink on `target` with the same raw, unresolved target string `readlink()` returned (dangling links and relative targets included, never validated/resolved); `"reject"` restores the exact old unconditional-raise behavior. If `target` can't create symlinks at all (every backend except `LocalPath` and `SftpPath`), `"preserve"` also raises `NotImplementedError`, through the same `ignore_error`/`hook()` machinery as every other sync branch, not a silent skip. **This is a default-behavior change, not a pure extension** -- flagged here because existing callers relying on the old unconditional raise (e.g. to detect and skip symlinks) must now pass `symlink_mode="reject"` explicitly. | Faithful one-way tree mirroring needs symlinks preserved as symlinks by default, not silently dropped/erroring -- discovered via a real cross-host sync use case (hostctl). `follow_symlinks=True` (unchanged default) still resolves through symlinks during traversal, so this only affects callers who already opted into `follow_symlinks=False`. **User decision, 2026-07-28.** |
| `S3Path` directories | N/A (pathlib directories are real filesystem entries) | `is_dir()` is prefix emulation (any object key under `"<path>/"`); `mkdir()` creates a zero-byte `"<path>/"` marker object; `rmdir()` requires no other keys under that prefix (pathlib-parity "must be empty"). If an exact object key and a `"<path>/"` prefix both exist, exact object operations such as `stat()` and `rm(recursive=True)` treat the path as the object first. | S3 has no native directory concept -- this is the same prefix convention the AWS console itself uses for an empty "folder". Exact-object precedence avoids deleting a prefix tree when the addressed path is a real object. |
| `GsPath`/`AzPath` directories | N/A (pathlib directories are real filesystem entries) | `is_dir()` is prefix emulation (any blob under `"<path>/"`); `mkdir()` creates a zero-byte `"<path>/"` marker blob; `rmdir()` requires no other blobs under that prefix (pathlib-parity "must be empty"). If an exact object/blob key and a `"<path>/"` prefix both exist, exact object operations such as `stat()` and `rm(recursive=True)` treat the path as the object first. | GCS and Azure Blob have no native directory concept -- same prefix emulation as `S3Path`. Exact-object precedence avoids deleting a prefix tree when the addressed path is a real object. |
| `GsPath`/`AzPath` `stat().st_mtime` | Real filesystem mtime | Always `0` | Querying just the mtime alone would require separate API calls beyond what the listing/get operations already provide. |
| `GitHubPath`/`GitLabPath` write methods (`mkdir`, `unlink`, `rmdir`, `rename`, `chmod`, `open("w")`) | pathlib supports all of these | All raise `NotImplementedError` -- read-only | Writing to a git repo goes through a commits API (create a commit, not a direct file write) with no filesystem-shaped equivalent (must specify a commit message/author, and typically targets a new branch) -- out of scope; revisit only with a concrete use case. |
| `GitHubPath`/`GitLabPath` `stat().st_mtime` | Real filesystem mtime | Always `0` | Neither REST API returns a last-modified timestamp from the same call that gives type/size -- that requires a separate, expensive per-path commit-history lookup. Same category as `ftp:`'s NLST-fallback limitation. |
| `GitHubPath` `symlink`/`submodule` tree entries | pathlib exposes `is_symlink()` | Surfaced as a plain file, no distinction | No portable meaning for a submodule (a pointer to another repo, not file content) or a symlink (git stores the link target as the blob content) without extra API calls; not implemented. |
| `empty_dir/` in a `github:`/`gitlab:` tree | pathlib directories can be empty | Requires a placeholder blob inside (e.g. `.gitkeep`) to exist at all | Git itself has no empty-directory concept -- neither API can return a tree entry for a path with zero blobs under it, so this isn't a library limitation, it's inherent to git. |
| `HttpPath` `open("a")` default append mode | POSIX `O_APPEND` is atomic (all appends serialized) | Default "rewrite" mode is non-atomic (GET existing + append in client memory + PUT full body) -- concurrent appenders can race | HTTP has no native append primitive; rewrite mode trades atomicity for universality (works on any server that supports PUT). Opt-in "patch" mode using `Content-Range` PATCH is atomic on servers that support it (use `with_session(..., append_mode="patch")`), but raises `PermissionError` if the server rejects it. |
| `Uri`/`UriPath.__str__()` | `str(pathlib.Path)` round-trips the full path | Drops the password from userinfo (`sftp://u:pw@h/p` -> `"sftp://u@h/p"` via `as_uri(sanitize=True)`) -- reparsing the result gives a *different*, unauthenticated URI | `str()` is what logging/printing reach for; a credentialed URI landing in a log line is worse than a `str()` that doesn't round-trip. Use `as_uri(sanitize=False)` for the full URI including credentials. |
| `Uri`/`UriPath.__fspath__()` | `os.fspath(pathlib.Path)` always succeeds with a locally-openable path | Raises `NotImplementedError` for any non-`file` scheme, **except** schemes with `_host_filesystem_path = True` (currently `sftp:`), which return `.path` -- a path meaningful on the URI's *own* host, not the local machine | `os.fspath()` has two consumers: "open this locally" (where returning a remote path would silently read the wrong file) and "build a command line for a process that runs on the path's host" (`subprocess`, remote executors) -- correct for the second consumer, wrong for the first. Schemes opt in via `_host_filesystem_path` only when `.path` genuinely is a host filesystem path. `host_fspath()` is the unambiguous accessor for the second use case: it never falls back to treating a path as local. **User decision, 2026-07-28.** |
| `uri.source.Source.__str__()`/`__repr__()` | `str()` previously called `uricompose()` with the raw `userinfo` -- a genuinely valid, connectable URI including the password; `repr()` used `NamedTuple`'s default, which also renders every field, including `userinfo`, verbatim | Both now redact the password from `userinfo` the same way `Uri.__str__()` does (`root:secret` -> `root`) -- `.userinfo`/`.parsed_userinfo()`/`["userinfo"]` (the actual data-access API) still return the real password; only display is sanitized. `str(source)` no longer reconstructs an authenticated URI -- use the new `Source.as_str(sanitize=False)` for the full round trip (mirrors `Uri.as_uri(sanitize=False)` exactly, same name/kwarg) | `repr()` is what a traceback frame renders, so a `Source` anywhere on a failing call stack used to put the password in the log even though `Uri.__str__()` already redacted -- a caller who saw `Uri` redact reasonably assumed the layer beneath it did too. Same rationale as the `Uri.__str__()` row above. **User decision, 2026-07-29.** |

## Explicitly out of scope (not implemented on `Pathname`/`Path`)

These `pathlib.Path` methods are **not** part of the generic `Pathname`/`Path`
contract because they don't have a portable meaning across arbitrary
URI/virtual backends (a `MemPath` or `http://` URL has no filesystem-relative
cwd, no symlinks, no OS-level owner/group). `LocalPath` gets every one of
these for free from `pathlib.Path` via MRO -- this list only describes what
`Uri`/`UriPath`/`MemPath` (and custom `Path` subclasses in general) don't get:

- `resolve()`, `absolute()` -- no portable notion of "the current working
  directory" or canonicalizing `..`/symlinks for an arbitrary backend.
- `readlink()`, `symlink_to()`, `hardlink_to()` -- no portable symlink/hardlink
  concept for most backends. **`sftp:` is the exception**: `SftpPath`
  implements `readlink()`/`symlink_to()` on both backends (core SFTPv3
  operations) and `hardlink_to()` on the asyncssh backend only (paramiko's
  `SFTPClient` has no hard-link operation at all -- `NotImplementedError`
  immediately, no server round trip). See the `sftp:` row's footnote in
  `guides/schemes.md`.
- `owner()`, `group()` -- no portable uid/gid-to-name mapping.
- `expanduser()`, `Path.cwd()`, `Path.home()` -- inherently tied to the local
  OS/filesystem, meaningless for a URI or in-memory path.
- `walk(..., follow_symlinks=True)` symlink-cycle protection -- `walk()`
  itself is implemented (see `Path.walk`), but cycle detection when following
  symlinks is not; only `LocalPath` (via pathlib) protects against symlink
  loops during a followed walk.

## Deliberate extensions (new methods/kwargs, not divergences)

These don't diverge from any existing pathlib behavior (pathlib has no
equivalent, or the kwarg is new/optional) -- listed for completeness, not
because a behavioral decision needed documenting:

- `joinpath(*args)`, `rglob(pattern)`, `full_match(pattern)` (3.13 parity),
  `anchor`/`drive`/`root` on `Pathname` (generic derivation: `root` is `"/"`
  when the first segment is empty, else `""`; `drive` is always `""`),
  `read_text(..., newline=)` (3.13 parity) -- all additive, no divergence.
- `Path.glob()`/`LocalPath.glob()`: `recursive=` defaults to auto-detect
  (`True` if the pattern contains a `"**"` component, else `False`) instead
  of pathlib's implicit-always-recursive-on-`**` with no override. Passing
  `recursive=False`/`True` explicitly always wins over the auto-detect.
  **User decision, 2026-07-11.** `include_hidden=`/`dironly=` are documented
  extensions beyond pathlib's `glob()` signature. **Caution:** on remote
  schemes (http/sftp), a recursive glob walks the whole remote subtree.
- `BinaryOpen.copy(target, *, progress=None, chunk_size=shutil.COPY_BUFSIZE)`
  / `Path.copy(target, ..., progress=None)`: optional progress-reporting
  hook, no pathlib equivalent. `BinaryOpen.copy()`'s `progress(bytes_copied,
  total_size)` fires after each chunk (`total_size` is `None` when the
  source doesn't implement `Stat` or `stat()` fails); `Path.copy()`'s
  `progress(path, bytes_copied, total_size)` adds the source `Path` being
  streamed, so a `recursive=True` copy can report per-file identity
  alongside byte progress. `chunk_size` is now caller-visible (previously
  hardcoded to `shutil.copyfileobj`'s default). `progress=None` (the
  default) is byte-for-byte identical to the prior `shutil.copyfileobj`
  behavior -- no per-chunk overhead when unused. **Known limitation:**
  native/batch transfer paths that bypass the generic streaming copy --
  currently only `SftpPath`'s asyncssh concurrent fan-out
  (`copy(recursive=True)` on a directory) -- do not invoke `progress`; this
  was a deliberate scope decision for the first cut (generic-stream-only),
  not an oversight. **2026-07-28.**
- `protocols.checksum.NativeChecksum` (`checksum(algorithm="md5") -> str`) --
  an entirely new, optional protocol with no pathlib equivalent. A `Path`
  subclass may implement it to compute a file digest server-side (e.g.
  `SftpPath` against an OpenSSH server's `check-file@openssh.com` SFTP
  extension) instead of streaming the content through `open("rb")`. Not
  mixed into the base `Path`/`Pathname` ABC -- most backends never
  implement it, and a plain `Path` has no `.checksum` attribute at all.
  **Hard contract, not a style choice:** an implementation MUST raise
  `NotImplementedError` (never return a value) when it cannot produce a
  genuine content digest under the requested `algorithm` -- this is what
  keeps `PathSyncer` (see its class docstring, `utils/sync.py`) from ever
  comparing a native digest to a streamed one under a mismatched algorithm,
  or trusting something hash-shaped but not actually a content hash (e.g.
  S3's ETag for a multipart upload, deliberately NOT implemented here for
  exactly that reason). `utils.checksum.md5`/`sha256`/`stream` (the
  pre-existing streaming helpers) are unaffected and keep working for
  direct callers that don't go through the protocol or `PathSyncer`.
  `supported_checksums() -> frozenset[str]` (default `frozenset()`) is a
  companion advisory capability query -- never raises, lets a caller pick a
  shared algorithm across two paths before calling anything expensive, but
  is advisory only: `checksum()`'s own `NotImplementedError` contract
  remains authoritative regardless of what this advertises.
- `PathSyncer(..., quick_check=True)` -- a new constructor kwarg, no
  pathlib equivalent. For any sync pair where at least one side is
  non-local, a metadata-only pre-check (`st_size` + `st_mtime`, from
  already-cached listing metadata, no extra round trip) skips the checksum
  call entirely when both already match; a mismatch always falls through
  to a real checksum rather than being treated as "changed" on its own.
  Local-to-local pairs never engage this pre-check. `quick_check=False`
  restores always-checksum behavior for non-local pairs too. **User
  decision, 2026-07-28.**
