# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/jose-pr/pathlib_next/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/jose-pr/pathlib_next/releases/tag/v0.4.0
[0.3.5]: https://github.com/jose-pr/pathlib_next/releases/tag/v0.3.5
