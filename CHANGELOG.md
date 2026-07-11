# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
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

[Unreleased]: https://github.com/jose-pr/pathlib_next/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/jose-pr/pathlib_next/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/jose-pr/pathlib_next/releases/tag/v0.4.0
[0.3.5]: https://github.com/jose-pr/pathlib_next/releases/tag/v0.3.5
