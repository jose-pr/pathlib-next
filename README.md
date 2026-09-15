# pathlib_next

[![Version](https://img.shields.io/pypi/v/pathlib-next.svg)](https://pypi.org/project/pathlib-next/)
[![Python versions](https://img.shields.io/pypi/pyversions/pathlib-next.svg)](https://pypi.org/project/pathlib-next/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/jose-pr/pathlib-next/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-latest-blue.svg)](https://jose-pr.github.io/pathlib-next/)
[![CI](https://img.shields.io/github/actions/workflow/status/jose-pr/pathlib-next/test.yml)](https://github.com/jose-pr/pathlib-next/actions/workflows/test.yml)

A **robust, extensible pathlib-like base** for any resource addressable as a
path or URI. Same method names, signatures, semantics and exception types as
`pathlib.Path` wherever a `pathlib.Path` equivalent exists: write code once
against `Path`/`UriPath` and it runs against your local disk, an in-memory
tree, an archive member, an HTTP or WebDAV server, SFTP/FTP, S3, Google Cloud
Storage, Azure Blob Storage or a GitHub/GitLab repository. Deliberate
differences from `pathlib` are listed in
[Divergences from pathlib](https://jose-pr.github.io/pathlib-next/divergences/).

## Features

| Scheme (class) | Read | Write | List | Stat | mkdir | Delete | rename | Extra |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| local path (`LocalPath`) | Yes | Yes | Yes | Yes | Yes | Yes | Yes | none |
| in-memory (`MemPath`, no URI scheme) | Yes | Yes | Yes | Yes | Yes | Yes | No (`move()` copies) | none |
| `file:` (`FileUri`) | Yes | Yes | Yes | Yes | Yes | Yes | Yes | `uri` |
| `data:` (`DataUri`, RFC 2397) | Yes | No | No | Yes | No | No | No | `uri` |
| `zip:` (`ZipUri`) | Yes | Local `file:` archive only | Yes | Yes | Local archive only | Local archive only | Local archive only | `uri` |
| `tar:` (`TarUri`, incl. gz/bz2/xz) | Yes | No | Yes | Yes | No | No | No | `uri` |
| `archive:`, `archive+zip:`, `archive+tar:` | As `zip:`/`tar:` for the detected or pinned format | | | | | | | `uri` |
| `ftp:` / `ftps:` (`FtpPath`) | Yes | Yes | Yes (MLSD, NLST fallback) | Yes | Yes | Yes | Yes | `uri` |
| `http:` / `https:` (`HttpPath`) | Yes | Yes (PUT, configurable) | Yes (HTML index) | Yes | No | Yes (DELETE) | No | `http` |
| `dav:` / `davs:` (`DavPath`, WebDAV) | Yes | Yes (no append) | Yes (PROPFIND) | Yes | Yes | Yes | Yes | `http` |
| `sftp:` (`SftpPath`) | Yes | Yes | Yes | Yes | Yes | Yes | Yes | `sftp` or `sftp-async` |
| `s3:` (`S3Path`) | Yes | Yes (no append) | Yes (prefixes) | Yes | Yes (marker object) | Yes | Yes (same bucket) | `s3` |
| `gs:` (`GsPath`) | Yes | Yes (no append) | Yes (prefixes) | Yes | Yes (marker object) | Yes | Yes (same bucket) | `gs` |
| `az:` (`AzPath`) | Yes | Yes (no append) | Yes (prefixes) | Yes | Yes (marker blob) | Yes | Yes (same container) | `az` |
| `github:` / `gitlab:` / `git:` | Yes | No | Yes | Yes | No | No | No | `http` |

Every scheme shares the same `glob()`, `walk()`, `copy()`/`move()`, `rm()` and
`PathSyncer` implementations. The per-scheme notes (timeouts, host-key and
certificate verification, authentication, limits) are in
[Schemes](https://jose-pr.github.io/pathlib-next/guides/schemes/).

- **One path interface** over local files, in-memory trees, archive members
  and every URI scheme above.
- **`MemPath`**: a virtual filesystem for tests, mocks and transient data.
- **`PathSyncer`**: one-way, content-compared tree sync between any two
  implementations, with dry runs and event hooks.
- **`uripath` command**: `read`, `write`, `cp`, `rm` and `sync` across any of
  these paths, with `-` for stdin/stdout.
- **Extensible two ways**: subclass `Path` for a non-URI resource, or
  `UriPath` for a new URI scheme, and verify it with the bundled pytest
  contracts.

## Installation

```bash
pip install pathlib-next
```

| Extra | Adds | Needed for |
| --- | --- | --- |
| `uri` | `uritools`, `netimps` | `pathlib_next.uri` and every URI scheme, including `file:`, `data:`, `ftp(s):` and archives |
| `http` | `requests` (+ `uri`) | `http(s):`, `dav(s):`, `github:`, `gitlab:`, `git:` |
| `sftp` | `paramiko` (+ `uri`) | `sftp:` with the paramiko backend |
| `sftp-async` | `asyncssh` (+ `uri`) | `sftp:` with the asyncssh backend |
| `s3` | `boto3` (+ `uri`) | `s3:` |
| `gs` | `google-cloud-storage` (+ `uri`) | `gs:` |
| `az` | `azure-storage-blob`, `azure-identity` (+ `uri`) | `az:` |

With no extras, `pathlib_next.Path`/`LocalPath`, `pathlib_next.mempath.MemPath`,
the utilities, the test contracts and `uripath` on local paths all work; the
`uri` extra is what makes `pathlib_next.uri` importable.

## Quick start

**Local filesystem**, as `pathlib.Path`:

```python
from pathlib_next import Path

p = Path("data") / "report.txt"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("hello")
print(p.read_text())
```

**In memory**, no disk I/O:

```python
from pathlib_next.mempath import MemPath

p = MemPath("/config/settings.json")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text('{"debug": true}')
print([child.name for child in p.parent.iterdir()])
```

**Any URI**: `UriPath` returns the class registered for the scheme:

```python
from pathlib_next.uri import UriPath

print(UriPath("file:data/report.txt").read_text())       # FileUri
print(UriPath("data:text/plain;base64,aGVsbG8=").read_text())  # DataUri
```

**Archive members**: `<scheme>:<archive-uri>!/<member>`, where the archive
itself is any URI (`file:`, `http:`, `sftp:`, ...):

```python
import zipfile
from pathlib_next.uri import UriPath

with zipfile.ZipFile("backup.zip", "w") as archive:
    archive.writestr("etc/config.ini", "[main]\n")

member = UriPath("zip:file:backup.zip!/etc/config.ini")
print(member.read_text())
print([child.name for child in member.parent.iterdir()])
```

**Remote paths** use the same API:

```python
from pathlib_next.uri import UriPath

for child in UriPath("https://example.com/data/").iterdir():
    print(child.name, child.stat().st_size)

print(UriPath("sftp://user@host/var/log/app.log").read_text())
print(UriPath("s3://bucket/reports/2026.csv").stat().st_mtime)
print(UriPath("github://github.com/owner/repo/README.md?ref=main").read_text())
```

**Copy and sync across schemes**:

```python
from pathlib_next import Path
from pathlib_next.mempath import MemPath
from pathlib_next.utils.sync import PathSyncer

source = MemPath("/site")
(source / "css").mkdir(parents=True)
(source / "index.html").write_text("<h1>hi</h1>")

PathSyncer(remove_missing=True).sync(source, Path("site-copy"))
print(sorted(p.name for p in Path("site-copy").iterdir()))
```

**Command line**:

```bash
uripath cp report.txt sftp://host/tmp/report.txt
uripath sync --dry-run --remove-missing site/ s3://bucket/site/
```

## Extending

Two first-class ways to add a path-addressable resource, covered with worked
examples in [Extending](https://jose-pr.github.io/pathlib-next/guides/extending/):

- Subclass `Path` directly for a non-URI resource (`MemPath` is the reference).
- Subclass `UriPath` and declare `__SCHEMES` for a new URI scheme; it is
  dispatched as soon as its module is imported, or through a
  `pathlib_next.schemes` entry point.

`pathlib_next.testing` provides the pytest contracts (`PurePathContract`,
`ReadPathContract`, `PathContract`) and `populate_fixture_tree()` used to
verify every built-in scheme.

## API overview

| Module | Purpose |
| --- | --- |
| `pathlib_next.path` | `Pathname`/`Path` base classes and the derived operations |
| `pathlib_next.fspath` | `LocalPath`, `PosixPathname`, `WindowsPathname` |
| `pathlib_next.mempath` | `MemPath` in-memory filesystem |
| `pathlib_next.protocols` | `Stat`, `Chmod`, `BinaryOpen`, `NativeChecksum` |
| `pathlib_next.uri` | `Uri`, `UriPath`, `Source`, `Query` |
| `pathlib_next.uri.schemes` | Built-in schemes: `file`, `data`, `zip`/`tar`/`archive`, `ftp`, `http`, `dav`, `sftp`, `s3`, `gs`, `az`, `github`, `gitlab`, `git` |
| `pathlib_next.utils` | `glob`, `sync` (`PathSyncer`), `stat` (`FileStat`), `checksum`, `archive` and helpers |
| `pathlib_next.testing` | pytest contracts for custom implementations |
| `pathlib_next.tools.uripath` | the `uripath` command |

The full reference is on the [documentation site](https://jose-pr.github.io/pathlib-next/);
an agent-oriented API summary ships inside the package as
`pathlib_next/AGENTS.md`.

## Supported Python versions

Python 3.9 through 3.14. CI runs the full suite on Ubuntu for every version
from 3.9 to 3.14, on Windows and macOS for 3.9 and 3.14, without any extras
on 3.9 and 3.14, and with the `gs`/`az` SDKs on 3.9 and 3.14.

## Development

```bash
pip install -e ".[dev,uri,http,sftp,sftp-async,s3,gs,az]"
python -m pytest -q -rs
```

Tests for an extra that is not installed are skipped, so check the skip list
(`-rs`). Contributor notes (virtual environments, formatting, CI, releases)
are in the repository's
[`AGENTS.md`](https://github.com/jose-pr/pathlib-next/blob/main/AGENTS.md);
benchmarks are described in
[Benchmarks](https://jose-pr.github.io/pathlib-next/benchmarks/). To preview
the documentation: `pip install -e ".[docs]"` then `mkdocs serve`.

Changes are recorded in the
[changelog](https://github.com/jose-pr/pathlib-next/blob/main/CHANGELOG.md).

## License

MIT; see [LICENSE](https://github.com/jose-pr/pathlib-next/blob/main/LICENSE).
