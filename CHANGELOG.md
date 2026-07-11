# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- README and docs landing page were still describing the pre-0.6.0 scheme set:
  the capability matrix, extras table, and quick starts now cover `data:`,
  `ftp(s):`, `zip:`/`tar:`, `dav(s):`, and `s3:` (all shipped in 0.6.0/0.7.0
  but previously only documented in the Schemes guide).

## [0.7.0] - 2026-07-11

### Added (Phase 7b new schemes, optional extras)
- `dav:`/`davs:` scheme (`pathlib_next.uri.schemes.webdav.DavPath`):
  extends `HttpPath` with WebDAV (RFC 4918) PROPFIND for real stat/listdir
  metadata (replacing HTML-index scraping) and PUT/DELETE/MKCOL/MOVE for
  full read/write access. Requests go to the equivalent `http:`/`https:`
  URL; `as_uri()` still reports `dav:`/`davs:`. Reuses the `http` extra,
  no new dependency. `rmdir()` is recursive by WebDAV spec, unlike
  `pathlib.Path.rmdir()`'s "must be empty" contract -- documented, not
  silent.
- `s3:` scheme (`pathlib_next.uri.schemes.s3.S3Path`,
  `s3://bucket/key/path`): read/write/list via `boto3`. New `s3` extra.
  S3 has no real directories -- `is_dir()` is prefix emulation (any
  object key under `"<path>/"`), `mkdir()` creates a zero-byte `"<path>/"`
  marker object, `rmdir()` requires no other keys under that prefix.
  `rename()` uses server-side `copy_object`+`delete_object` (same-bucket
  only) instead of the generic download+upload+delete `move()` fallback.

## [0.6.0] - 2026-07-11

### Added (Phase 7a new schemes, stdlib-only, no new deps)
- `data:` scheme (RFC 2397, `pathlib_next.uri.schemes.data.DataUri`):
  read-only, no backend/connection -- the entire file content is embedded
  in the URI (`data:[<mediatype>][;base64],<data>`). `stat().st_size` is
  the decoded payload length; `iterdir()` raises `NotADirectoryError`
  (it's always a single file); write operations raise `NotImplementedError`.
- `ftp:`/`ftps:` scheme (`pathlib_next.uri.schemes.ftp.FtpPath`): full
  read/write/list access via stdlib `ftplib`, with a thread-keyed LRU
  connection cache mirroring `sftp.py`. Listing/stat prefer MLSD (RFC
  3659); servers without it fall back to NLST (listing) and SIZE
  (file-only stat). Writes buffer in memory and upload via STOR/APPE on
  `close()`. `chmod()` uses the common but non-standard `SITE CHMOD`
  extension (may not be supported by every server).
- `zip:`/`tar:` archive paths (`pathlib_next.uri.schemes.archive`):
  `<scheme>:<archive-uri>!/<inner-path>` (Java-style `!/` separator,
  URI form proposed to and confirmed by the user before implementation).
  The archive half is itself any absolute URI with an explicit scheme, so
  archives are readable straight off any other backend (`file:`, `http:`,
  `sftp:`, `ftp:`, `data:`, ...). Read is supported for both schemes.
  Write is `zip:`-only, and only for brand-new entries in a local (`file:`)
  outer archive (overwriting/deleting/renaming an existing entry would
  need a full-archive rewrite -- not implemented, raises
  `NotImplementedError`). `tar:` auto-detects `.tar.gz`/`.tar.bz2`/`.tar.xz`
  compression and is always read-only.

## [0.5.0] - 2026-07-11

### Fixed (critical -- found while writing Phase 6 examples)
- `Path("...")` -- the top-level dispatcher documented in this project's
  own README quick start and used throughout -- silently dropped its
  constructor arguments on Python <3.12, leaving a blank instance that
  crashed with `AttributeError: _drv` the moment anything touched it (e.g.
  the `/` operator). Masked on 3.12+, where the real parsing happens in
  `__init__` (called separately, with the original args, regardless of what
  `__new__` did) rather than `__new__` itself. Every one of Phase 5's 300
  tests constructed via `LocalPath(...)` directly instead, so this went
  undetected until `examples/local_and_mem.py` exercised the documented
  `Path(...)` entry point end to end.

### Fixed (found by the new Phase 5 test suite, not in the original bug list)
- `LocalPath.stat()`/`chmod()` inherit directly from `pathlib.Path` via MRO
  and crashed with `TypeError` on Python 3.9 the moment anything passed
  `follow_symlinks=` (e.g. `Path.walk()`'s default `follow_symlinks=False`)
  -- now shimmed with `lstat()`/`lchmod()` on <3.10, same as the existing
  `FileUri` shim (which now just delegates to `LocalPath`).
- `MemPath.__init__` decided whether to propagate a parent's backend with
  `if _backend and backend is None:` -- an empty (but valid) backend dict is
  falsy, so joining off a freshly-created, empty `MemPath` silently gave the
  child a disconnected new backend instead of sharing the parent's.
- `MemPath.stat()` never set `st_size` for files (always defaulted to `0`),
  breaking any size-based checksum comparison (notably `PathSyncer`'s
  typical usage).
- `glob()`'s core algorithm decided whether to recurse into the *parent*
  directory using whether the *leaf* segment is a wildcard, instead of
  whether the *parent path itself* contains one. Since a wildcarded leaf
  with a literal parent directory is the overwhelmingly common case
  (`glob("*.py")`), this always took the "recurse into parent" branch,
  which only degenerated back to the correct single directory when the
  parent has a non-empty literal name to re-match against -- true for
  essentially every real filesystem path except an OS root. It silently
  returned the wrong result on `MemPath`'s virtual root (empty name).
- `HttpPath.iterdir()` gave every subdirectory entry an empty `.name`:
  directory-listing entries for subdirectories carry a trailing `/`
  (`htmllistparse`'s convention), which wasn't stripped before building the
  child's path, and `Pathname.name` derives from the last path segment --
  empty for a trailing-slash path.
- `SftpPath.rename()` resolved a plain string target relative to `self`
  (joining it as a child, e.g. `"/a.txt".rename("b.txt")` produced
  `"/a.txt/b.txt"`) instead of `self`'s parent (sibling rename).

### Added (test suite)
- Full pytest suite (`tests/`): pure-path parity against `pathlib.PurePosixPath`
  (`test_parity_pure.py`), local I/O parity against `pathlib.Path`/`os.walk`
  (`test_parity_io.py`), a reusable filesystem-contract mixin run against
  `LocalPath`/`MemPath`/`FileUri` and exported as `pathlib_next.testing.
  PathContract` for third-party `Path`/`UriPath` implementers
  (`test_contract.py`), glob vs. stdlib ground truth (`test_glob.py`), URI
  parsing/scheme-dispatch/query/source coverage, MemPath- and SFTP-specific
  unit tests (SFTP mocked, no real server), HTTP tests against a real stdlib
  `ThreadingHTTPServer`, and `PathSyncer` coverage. 300 tests, ~85% line
  coverage, green on both Python 3.9 and 3.13.

### Added (docs)
- `docs/guides/schemes.md` (capability matrix per scheme) and
  `docs/guides/extending.md` (both extension tracks, with worked examples
  and `pathlib_next.testing.PathContract` usage). Rewrote `docs/index.md`
  and the README with a 30-second example per scheme and a capability
  matrix. Class-level docstrings added across the package for the rendered
  API reference.

### Changed
- `examples/example.py` (an unstructured scratch script) split into three
  focused, runnable examples: `examples/local_and_mem.py` (self-contained,
  no network), `examples/http_listing.py` and `examples/sftp_sync.py`
  (network-touching, guarded under `if __name__ == "__main__"`,
  configurable via env vars, fail soft when unreachable/unconfigured).

### Added
- `Pathname.joinpath()`, `Pathname.full_match()` (3.13 parity, supports `**`
  matching any number of segments), `Pathname.anchor`/`drive`/`root`
  (generic derivation for non-local paths), `Path.rglob()`,
  `read_text(..., newline=)` (3.13 parity), `Path.samefile()` (default
  `st_dev`/`st_ino` comparison when the backend's `stat()` provides them,
  `NotImplementedError` otherwise).
- `Path.glob()`/`LocalPath.glob()`: `recursive=` now auto-detects (`True` if
  the pattern has a `"**"` component) instead of defaulting to `False`;
  explicit `recursive=True`/`False` still overrides.
- `Path.copy()`: raises `IsADirectoryError` when the target is an existing
  directory (previously misbehaved); gained `follow_symlinks=`/
  `preserve_metadata=` kwargs, named to match CPython 3.14's `Path.copy()`.
- `docs/divergences.md`: registry of every deliberate behavioral divergence
  from `pathlib`, with rationale. Linked from the docs nav.

### Fixed
- `Path.mkdir(parents=True)` created intermediate parents with `exist_ok=False`
  (racy, and wrong when a parent already existed) and dropped the caller's
  `exist_ok` on the final retry.
- `Path.touch(exist_ok=False)` silently truncated an existing file instead of
  raising `FileExistsError` (pathlib parity).
- `LocalPath.glob()`'s `dironly` parameter defaulted to `False`, which made the
  `is None` check for trailing-slash directory-only detection dead code.
- `Stat._st_mode()` only caught `FileNotFoundError`, letting `PermissionError`
  and other `OSError`s propagate out of `exists()`/`is_dir()`/etc. where pathlib
  returns `False`. Also fixed: `follow_symlinks` was accepted but never
  forwarded to the underlying `stat()` call, so `is_symlink()` never actually
  inspected the symlink itself.
- `MemPath._open()` treated any mode other than `"w"` as a read, so `"a"`/`"x"`
  silently misbehaved; now dispatches `r`/`w`/`x`/`a` correctly and raises
  `NotImplementedError` for anything else. `MemBytesIO.close()` used
  `seek(0);read()` instead of `getvalue()`, losing content if the caller's
  cursor wasn't already at position 0 when closing.
- `MemPath.normalized` mangled `".."`-escaping paths (e.g. `".."`) into `"."`;
  now normalizes against a virtual root so they clamp at the root instead.
- `PathAndStat.__getattr__()` returned `None` for any unrecognized attribute
  instead of raising `AttributeError`, breaking `hasattr()`-based logic.
- `parsedate(None)` / an unparseable date string returned "now" instead of
  epoch 0, which could poison `PathSyncer`'s checksum/freshness comparisons for
  HTTP sources with no `Last-Modified` header.
- `HttpPath.stat()` used a bare `except:`; cached `_isdir` from a response that
  hadn't been confirmed successful yet (including 404s); and didn't fall back
  to GET when a server rejected `HEAD` with 405.
- `uri.Query` no longer depends on `uritools`' private `_querydict`/`_querylist`
  helpers (reimplemented locally against the public `uriencode()`).
- `Uri` join (`_load_parts`): `query`/`fragment` are now resolved with the same
  "last segment that actually sets one wins" rule already used for `source`
  (previously any segment, even one with no query/fragment, would blank out an
  earlier segment's). Join semantics are now documented explicitly:
  pathlib-`joinpath`-like, not RFC 3986 reference resolution, `..` is never
  resolved during join.
- `Source.is_local()` (DNS lookup) and `get_machine_ips()` are now
  `functools.lru_cache`d -- previously ran on every call.

### Fixed (crash-level bugs)
- `MemPath.stat()`/`MemPath._open()` returned a `FileNotFoundError` instance instead
  of raising it for a missing path, causing an unrelated `AttributeError` downstream.
- `LRU.invalidate()` called `self.lock()` instead of using `self.lock` as a context
  manager (`RLock` isn't callable) -- broke the SFTP client reconnect path.
- `Pathname.match()` had reversed `isinstance()` arguments and compared against
  `str(self)` (which includes scheme/host for `Uri`) instead of `as_posix()`.
- Glob wildcard detection (`WILCARD_PATTERN`, renamed `WILDCARD_PATTERN`, old name
  kept as an alias) used `.match()` (anchored) instead of `.search()`, so patterns
  like `"foo*"` weren't recognized as wildcards.
- `Uri` was unhashable (defined `__eq__` without `__hash__`); `__eq__` now also
  returns `NotImplemented` for non-`Pathname`/`str` operands instead of raising.
- `Uri.is_relative_to()` used `str.startswith()` on normalized path strings, so
  `/foo/bar2` was incorrectly reported as relative to `/foo/bar`; now compares
  path segments.
- `Uri.relative_to(walk_up=True)` was dead code -- an early guard raised
  `ValueError` before the walk-up loop ever ran.
- `HttpPath.is_dir()`/`is_file()` tested truthiness of bound methods
  (`self._is_dir`, `self.is_dir`) instead of calling/checking the right attribute,
  so both always returned truthy nonsense.
- `SftpPath.chmod()` didn't accept `follow_symlinks=`, so the inherited `lchmod()`
  crashed with `TypeError`; now raises `NotImplementedError` for
  `follow_symlinks=False` (paramiko has no `lchmod`).
- `SftpPath` defined `_rename()`, which nothing ever called -- renamed to
  `rename()` so `move()`/`rename()` actually use SFTP's native rename instead of
  silently falling back to copy+unlink for every move.
- `Uri.__init__()` used a bare `except:` around `Path.as_uri()` (now
  `except ValueError:`, matching what `as_uri()` actually raises for relative
  paths) and crashed with `AttributeError` when constructing from an
  `os.PathLike` that only implements `__fspath__` (no `as_posix()`).
- `Path.rm(ignore_error=callable)` never actually called the callable -- both
  branches of its error handler returned the callable object itself.

### Fixed (Python 3.9/3.10 compatibility)
- Actual Python 3.9/3.10 runtime compatibility (CI previously only tested 3.11/3.13
  and missed these): `LocalPath`/`Uri` case-sensitivity and path-separator detection
  crashed on 3.9-3.11 (`_flavour` object has no `normcase`); `open(mode="r")` crashed
  on <3.10 (`io.text_encoding` is 3.10+); glob pattern compilation crashed on <3.11
  (`re.NOFLAG` is 3.11+); `FileUri.stat()`/`chmod()` crashed on 3.9
  (`pathlib.Path.stat/chmod` gained `follow_symlinks=` in 3.10; raises
  `NotImplementedError` there for `follow_symlinks=False`).
- `LocalPath._path_separators` returned the env-var list separator (`;`/`:`) instead
  of the path separator, and could include a `None` altsep on POSIX.

### Added
- `tests/test_smoke.py`: regression coverage for README/example snippets across
  supported Python versions.

## [0.4.1] - 2026-07-11

### Fixed
- Removed explicit `[tool.hatch.build.targets.wheel]` packages config that caused hatchling to fail resolving `README.md` during editable installs on CI.
- Converted `README.md` from a symlink (mode `120000`) to a regular file, fixing `git checkout` failures on macOS and Windows runners.
- Removed agent-tooling references (`AGENTS.md`, `PYTHON.md`) from committed files.

## [0.4.0] - 2026-07-11

### Added
- Standardized repository layout and relocated examples to `examples/` directory.
- Configured MkDocs documentation site with dynamic API reference using `mkdocstrings`.
- Added GitHub Actions workflows for matrix testing (`test.yml`) and release pipelines (`release.yml`).
- Added typing marker `py.typed` for PEP 561 compliance.

### Changed
- Added backward compatibility support for Python 3.9 and 3.10: added `from __future__ import annotations` across the codebase, refactored runtime-evaluated union types to use `typing.Union`, and provided fallbacks for `TypeAlias` and `ParamSpec`.
- Updated package requirement to `requires-python = ">=3.9"`.

## [0.3.5] - 2026-07-11

### Added
- Split path into protocols that can be standalone.
- Sync error handling.
- Generic Path Protocol based pathlib implementation for URI paths with file access support for sftp, http, file schemes.

[Unreleased]: https://github.com/jose-pr/pathlib_next/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/jose-pr/pathlib_next/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/jose-pr/pathlib_next/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/jose-pr/pathlib_next/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/jose-pr/pathlib_next/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/jose-pr/pathlib_next/releases/tag/v0.4.0
[0.3.5]: https://github.com/jose-pr/pathlib_next/releases/tag/v0.3.5
