# pathlib_next

[![Version](https://img.shields.io/pypi/v/pathlib_next.svg)](https://pypi.org/project/pathlib_next/)
[![Python versions](https://img.shields.io/pypi/pyversions/pathlib_next.svg)](https://pypi.org/project/pathlib_next/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-latest-blue.svg)](https://jose-pr.github.io/pathlib_next/)
[![CI](https://img.shields.io/github/actions/workflow/status/jose-pr/pathlib_next/test.yml)](https://github.com/jose-pr/pathlib_next/actions/workflows/test.yml)

A **robust, extensible pathlib-like base** for any resource addressable as a
path or URI. Same method names, signatures, semantics, and exception types as
`pathlib.Path` wherever a `pathlib.Path` equivalent exists -- write code once
against `Path`/`UriPath` and it works against your local disk, an in-memory
tree, an HTTP index, or an SFTP server. Every intentional divergence from
`pathlib`'s behavior is documented, not silent -- see
[`docs/divergences.md`](https://jose-pr.github.io/pathlib_next/divergences/).

## Features

| Scheme | Read | Write | List | Stat | mkdir | Delete | rename | Extra required |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `LocalPath` / `file:` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | none |
| `mem:` (`MemPath`) | Yes | Yes | Yes | Yes | Yes | Yes | No | none |
| `data:` (RFC 2397) | Yes | No | No | Yes | No | No | No | none |
| `zip:` / `tar:` (archive `!/` paths) | Yes | zip: new entries, local archive | Yes | Yes | zip: local | No | No | none |
| `ftp(s):` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | none |
| `http(s):` | Yes | No | Yes (HTML index) | Yes | No | No | No | `http` |
| `dav(s):` (WebDAV) | Yes | Yes | Yes (PROPFIND) | Yes | Yes | Yes | Yes | `http` |
| `sftp:` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | `sftp` |
| `s3:` | Yes | Yes | Yes (prefix emulation) | Yes | Yes | Yes | Yes (same bucket) | `s3` |

Every scheme shares the same `glob()`, `walk()`, `copy()`/`move()`, `rm()`
implementations -- see the full matrix and notes in
[Schemes](https://jose-pr.github.io/pathlib_next/guides/schemes/).

- **Unified path interface** across local files, in-memory paths, archive
  members, and `file`/`data`/`ftp`/`http`/`dav`/`sftp`/`s3` URIs.
- **`MemPath`** -- a lightweight virtual filesystem for mocks, tests, or
  transient storage.
- **`PathSyncer`** -- one-way checksum-driven tree sync between any two
  `Path` implementations, with dry-run and event hooks.
- **`Query`/`Source`** -- parse and serialize URL query strings and URI
  authority components.
- **Extensible two ways**: subclass `Path` directly for a custom
  non-URI resource, or subclass `UriPath` for a new URI scheme -- see
  [Extending](https://jose-pr.github.io/pathlib_next/guides/extending/).

## Installation

```bash
pip install pathlib_next
```

Optional features/extras:

| Extra/flag | Adds | Needed for |
| --- | --- | --- |
| `uri` | `uritools` | URI parsing (any `UriPath` scheme) |
| `http` | `requests` | `http(s):` and `dav(s):` (WebDAV) paths |
| `sftp` | `paramiko` | `sftp:` path operations and transfers (sync backend) |
| `sftp-async` | `asyncssh` | `sftp:` path operations via the asyncssh backend instead (see `guides/schemes.md`'s `sftp:` row for selection precedence) |
| `s3` | `boto3` | `s3://bucket/key` paths |

`import pathlib_next` and `LocalPath`/`MemPath` work with no extras
installed; `data:`, `ftp(s):`, and `zip:`/`tar:` only need the `uri` extra
(they're stdlib-based otherwise).

## Quick start

**Local filesystem** -- drop-in `pathlib.Path`:

```python
from pathlib_next import Path

p = Path("./data") / "report.txt"
p.write_text("hello")
print(p.read_text())
```

**In-memory** (`mem:`) -- a virtual filesystem, no disk I/O:

```python
from pathlib_next.mempath import MemPath

p = MemPath("/config/settings.json")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text('{"debug": true}')
```

**`file:`** -- the same local filesystem, addressed as a URI:

```python
from pathlib_next.uri import UriPath

p = UriPath("file:./data/report.txt")
print(p.read_text())
```

**`http(s):`** -- read files and list Apache/nginx-style directory indexes:

```python
from pathlib_next.uri import UriPath

p = UriPath("http://example.com/data/")
for child in p.iterdir():
    if child.is_file():
        print(child.name, child.stat().st_size)
```

**`sftp:`** -- same interface, over SSH:

```python
from pathlib_next.uri import UriPath

p = UriPath("sftp://user@host/var/log/app.log")
print(p.read_text())
```

**`zip:`/`tar:`** -- address a member *inside* an archive (Java-style `!/`
separator; the archive half is itself any URI -- `file:`, `http:`, `sftp:`, ...):

```python
from pathlib_next.uri import UriPath

member = UriPath("zip:file:./backup.zip!/etc/config.ini")
print(member.read_text())
```

Also built in: `data:` (RFC 2397 inline payloads), `ftp(s):` (stdlib
`ftplib`), `dav(s):` (WebDAV, full read/write over HTTP), and `s3:`
(`boto3`) -- one example per scheme in
[Schemes](https://jose-pr.github.io/pathlib_next/guides/schemes/).

## Extending

Two first-class ways to add a new path-addressable resource -- both covered
in depth, with worked examples, in
[Extending](https://jose-pr.github.io/pathlib_next/guides/extending/):

- Subclass `Path` directly for a custom, non-URI resource (`MemPath` is the
  reference exemplar).
- Subclass `UriPath` and set `__SCHEMES` for a new URI scheme (`FileUri`/
  `HttpPath`/`SftpPath` are the built-in examples).

`pathlib_next.testing` provides reusable pytest mixins (`PurePathContract`, `ReadPathContract`, and `PathContract`) covering the baseline contracts for various levels of capabilities -- subclass one of them with a `root` fixture to verify your own implementation.

## API overview

| Module/Package | Purpose |
| --- | --- |
| `pathlib_next.path` | Base Path implementation and protocols |
| `pathlib_next.uri` | URI/URL specific path support and Query utils |
| `pathlib_next.uri.schemes` | Built-in schemes: `file`, `data`, `ftp`, `zip`/`tar`, `http`, `dav`, `sftp`, `s3` |
| `pathlib_next.mempath` | In-memory transient path structure |
| `pathlib_next.utils.sync` | Synchronization functions and PathSyncer class |
| `pathlib_next.testing` | `PathContract`, a pytest mixin for verifying custom implementations |

## Supported Python versions

Python >= 3.9, tested on 3.9 and 3.13 in CI (see
[`.github/workflows/test.yml`](.github/workflows/test.yml)).

## Development

```bash
pip install -e ".[dev,uri,http,sftp,sftp-async]"
pytest -q
```

If you maintain separate virtual environments per Python version locally
(e.g. `.venv/3.9/`, `.venv/3.13/`), run the same `pytest -q` in each --
CI does the equivalent across Python 3.9/3.13 on Linux, macOS, and Windows.

### Benchmarks

Run the benchmark suite using:
```bash
python benchmarks/bench.py
```

A benchmark report and methodology notes live in
[`docs/benchmarks.md`](docs/benchmarks.md).

### Releasing

This project follows [Semantic Versioning](https://semver.org/) and keeps a
[`CHANGELOG.md`](CHANGELOG.md). Pushing a tag matching `v*` triggers the release
workflow: test gate → build → publish → docs deploy.

### Documentation site

MkDocs builds the API reference from `docs/`, published on every
release. To preview locally: `mkdocs serve`.

## License

MIT — see [LICENSE](LICENSE).
