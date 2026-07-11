# pathlib_next

[![Version](https://img.shields.io/pypi/v/pathlib_next.svg)](https://pypi.org/project/pathlib_next/)
[![Python versions](https://img.shields.io/pypi/pyversions/pathlib_next.svg)](https://pypi.org/project/pathlib_next/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-latest-blue.svg)](https://jose-pr.github.io/pathlib_next/)
[![CI](https://img.shields.io/github/actions/workflow/status/jose-pr/pathlib_next/test.yml)](https://github.com/jose-pr/pathlib_next/actions/workflows/test.yml)

Generic Path Protocol based pathlib implementation for URI paths with file access support for sftp, http, file schemes.

## Features

- **Unified URI Interface** — Access resources on sftp, http, or local file schemes using python's familiar pathlib syntax.
- **In-memory filesystem** — `MemPath` provides a lightweight, virtual file system helper for mock files, testing, or transient storage.
- **Path synchronization** — `PathSyncer` allows syncing directory structures across different paths with customizable checksums.
- **Query & Source Parsing** — Parse and serialize complex URL query strings and URI sources easily.

## Installation

```bash
pip install pathlib_next
```

Optional features/extras:

| Extra/flag | Adds | Needed for |
| --- | --- | --- |
| `uri` | `uritools` | URI parsing capabilities |
| `http` | `requests`, `bs4`, `htmllistparse` | Read and list files over HTTP/HTTPS |
| `sftp` | `paramiko` | SFTP path operations and transfers |

## Quick start

### Unified Path Operations

```python
from pathlib_next import Path
from pathlib_next.uri import UriPath

# Use the unified path interface
local_path = Path("./my_folder")
http_path = UriPath("http://example.com/data.txt")

# Read and print text if it exists
if http_path.exists():
    print(http_path.read_text())
```

## API overview

| Module/Package | Purpose |
| --- | --- |
| `pathlib_next.path` | Base Path implementation and protocols |
| `pathlib_next.uri` | URI/URL specific path support and Query utils |
| `pathlib_next.mempath` | In-memory transient path structure |
| `pathlib_next.utils.sync` | Synchronization functions and PathSyncer class |

## Supported Python versions

Python >= 3.9

## Development

For environment setup, dependency installation, and running tests, refer to virtual environment configurations and running `pytest`.

### Releasing

This project follows [Semantic Versioning](https://semver.org/) and keeps a
[`CHANGELOG.md`](CHANGELOG.md). Pushing a tag matching `v*` triggers the release
workflow: test gate → build → publish → docs deploy.

### Documentation site

MkDocs builds the API reference from `docs/`, published on every
release. To preview locally: `mkdocs serve`.

## License

MIT — see [LICENSE](LICENSE).
