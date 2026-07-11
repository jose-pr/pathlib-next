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

| Method | pathlib behavior | Our behavior | Why |
| --- | --- | --- | --- |
| `Uri("a").parent` | `PurePosixPath("a").parent == PurePosixPath(".")` | `Uri("a").parent` has path `""` (`Uri("")`, which round-trips) | `Uri` has no cwd-relative concept of `"."` -- an empty path is the URI-natural "no path" representation. Changing this would make `Uri("")` non-idempotent under `.parent`. |
| `with_name()` / `with_suffix()` / `with_stem()` on `Uri`/`UriPath` | N/A (pathlib has no query/fragment) | Preserve the URI's query and fragment (implemented via `with_path`, which carries them over) | Deliberate extension: `UriPath("http://h/a?x=1").with_suffix(".txt")` keeping `?x=1` matches how most callers actually want to retarget just the path component of a URL. **User decision, 2026-07-11.** |
| `Path.__iter__` | `pathlib.Path` is not iterable (no `__iter__`) | `iter(path)` is `path.iterdir()` | Deliberate extension for ergonomic `for child in path:` loops. **Caution:** on remote schemes (http/sftp) this is a network call. **User decision, 2026-07-11.** |
| `Path.copy(target, ...)` | CPython 3.14 `Path.copy(target, *, follow_symlinks=True, dirs_exist_ok=False, preserve_metadata=False)`; always raises if `target` exists | Ours predates 3.14. Signature: `copy(target, *, overwrite=False, follow_symlinks=True, preserve_metadata=True)`. `overwrite=True` unlinks an existing non-directory target first; `preserve_metadata` defaults to **True** (opposite of 3.14) and only propagates `st_mode`, not timestamps/xattrs | Argument names aligned with 3.14 where cheap; `preserve_metadata=True` default kept for backward compat with this method's pre-existing (pre-3.14-alignment) behavior of always copying the mode bits. Full metadata preservation (timestamps, xattrs) is not implemented. |
| `Path.move(target, ...)` | Not in `pathlib` at all | Our own extension: tries `rename()`, falls back to copy+unlink | N/A -- pure extension, no pathlib method to diverge from. |
| `Path.rm(recursive=, missing_ok=, ignore_error=)` | Not in `pathlib` (closest: `shutil.rmtree`) | Our own extension | N/A -- pure extension. |
| `PathSyncer` / `Query` / `Source` | N/A | Our own extensions | N/A -- pure extensions, no pathlib equivalent. |
| `DavPath.rmdir()` | `pathlib.Path.rmdir()` requires the directory to be empty, raises `OSError` otherwise | WebDAV `DELETE` on a collection is recursive by spec (RFC 4918) -- deletes non-empty collections too | Not our choice -- inherent to the WebDAV protocol's `DELETE` semantics; no separate "delete if empty" verb exists to build an empty-check on top of. **Caution**, not a feature. |
| `S3Path` directories | N/A (pathlib directories are real filesystem entries) | `is_dir()` is prefix emulation (any object key under `"<path>/"`); `mkdir()` creates a zero-byte `"<path>/"` marker object; `rmdir()` requires no other keys under that prefix (pathlib-parity "must be empty", unlike `DavPath.rmdir()` above) | S3 has no native directory concept -- this is the same prefix convention the AWS console itself uses for an empty "folder". |

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
  concept (sftp *could* support these via paramiko, but it's not implemented).
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
