# pathlib_next

A **robust, extensible pathlib-like base** for any resource addressable as a
path or URI. Same method names, signatures, semantics, and exception types as
`pathlib.Path` wherever a `pathlib.Path` equivalent exists -- write code
against `Path`/`UriPath` once, and it works against your local disk, an
in-memory tree, an HTTP index, or an SFTP server.

Every intentional divergence from `pathlib`'s behavior is documented, not
silent -- see [Divergences from pathlib](divergences.md).

## Installation

```bash
pip install pathlib_next
```

| Extra | Adds | Needed for |
| --- | --- | --- |
| `uri` | `uritools` | `Uri`/`UriPath` parsing |
| `http` | `requests`, `bs4`, `htmllistparse` | `http://`/`https://` paths |
| `sftp` | `paramiko` | `sftp://` paths |

`import pathlib_next` and `pathlib_next.LocalPath`/`MemPath` work with **no
extras installed** -- extras are only needed for the URI schemes that use
them.

## 30-second tour

**Local filesystem** -- drop-in `pathlib.Path`:

```python
from pathlib_next import Path

p = Path("./data") / "report.txt"
p.write_text("hello")
print(p.read_text())
```

**In-memory** -- a virtual filesystem for tests/mocks, no disk I/O:

```python
from pathlib_next.mempath import MemPath

p = MemPath("/config/settings.json")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text('{"debug": true}')
```

**HTTP** -- read files and list Apache/nginx-style directory indexes:

```python
from pathlib_next.uri import UriPath

p = UriPath("http://example.com/data/")
for child in p.iterdir():
    if child.is_file():
        print(child.name, child.stat().st_size)
```

**SFTP** -- same interface, over SSH:

```python
from pathlib_next.uri import UriPath

p = UriPath("sftp://user@host/var/log/app.log")
print(p.read_text())
```

All four are the *same* `Path` contract -- `exists()`, `is_dir()`,
`iterdir()`, `glob()`, `read_text()`/`write_text()`, `copy()`/`move()`,
`rm()`, all behave the same way regardless of backend (capability
differences, e.g. `http:` being read-only, are listed in
[Schemes](guides/schemes.md)).

## Where to go next

- **[Divergences from pathlib](divergences.md)** -- every deliberate
  behavioral difference from `pathlib.Path`, with rationale.
- **[Schemes](guides/schemes.md)** -- capability matrix (read/write/list/
  stat/mkdir/delete/rename) per scheme.
- **[Extending](guides/extending.md)** -- add your own path type, two ways:
  subclass `Path` directly, or subclass `UriPath` for a new URI scheme.
- **[API Reference](api/reference.md)** -- generated from docstrings.
- **[Changelog](changelog.md)**.
