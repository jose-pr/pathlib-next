# pathlib_next

A **robust, extensible pathlib-like base** for any resource addressable as a
path or URI. Same method names, signatures, semantics and exception types as
`pathlib.Path` wherever a `pathlib.Path` equivalent exists: write code against
`Path`/`UriPath` once and it works against your local disk, an in-memory tree,
an archive member, HTTP/WebDAV, FTP/SFTP, S3, Google Cloud Storage, Azure Blob
Storage or a GitHub/GitLab repository.

Deliberate behavioral differences from `pathlib` are listed, with their
reasons, in [Divergences from pathlib](divergences.md).

## Installation

```bash
pip install pathlib-next
```

| Extra | Adds | Needed for |
| --- | --- | --- |
| `uri` | `uritools`, `netimps` | `pathlib_next.uri` and every URI scheme (`file:`, `data:`, `ftp(s):`, archives included) |
| `http` | `requests` | `http(s):`, `dav(s):`, `github:`, `gitlab:`, `git:` |
| `sftp` | `paramiko` | `sftp:` with the paramiko backend |
| `sftp-async` | `asyncssh` | `sftp:` with the asyncssh backend |
| `s3` | `boto3` | `s3:` |
| `gs` | `google-cloud-storage` | `gs:` |
| `az` | `azure-storage-blob` | `az:` |

Every scheme extra also installs `uri`. With **no extras**,
`pathlib_next.Path`/`LocalPath`, `pathlib_next.mempath.MemPath`, the utilities,
the test contracts and the `uripath` command on local paths all work.

## Tour

**Local filesystem**, as `pathlib.Path`:

```python
from pathlib_next import Path

p = Path("data") / "report.txt"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("hello")
print(p.read_text())
```

**In memory**, a virtual filesystem for tests and mocks:

```python
from pathlib_next.mempath import MemPath

p = MemPath("/config/settings.json")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text('{"debug": true}')
print(p.stat().st_size, [child.name for child in p.parent.iterdir()])
```

**URIs**: `UriPath` returns the class registered for the scheme:

```python
from pathlib_next.uri import UriPath

report = UriPath("file:data/report.txt")
print(type(report).__name__, report.read_text())

inline = UriPath("data:text/plain;base64,aGVsbG8=")
print(type(inline).__name__, inline.read_text())
```

**Archives**: a member inside a zip or tar, the archive itself addressed by any
URI:

```python
import zipfile
from pathlib_next.uri import UriPath

with zipfile.ZipFile("backup.zip", "w") as archive:
    archive.writestr("etc/config.ini", "[main]\n")

member = UriPath("zip:file:backup.zip!/etc/config.ini")
print(member.read_text())
```

**Remote schemes** share the same contract (these need a reachable server):

```python
from pathlib_next.uri import UriPath

for child in UriPath("https://example.com/data/").iterdir():
    print(child.name, child.stat().st_size)

print(UriPath("sftp://user@host/var/log/app.log").read_text())
```

`exists()`, `is_dir()`, `iterdir()`, `glob()`, `walk()`,
`read_text()`/`write_text()`, `copy()`/`move()` and `rm()` behave the same way
on every backend; what a backend cannot do (for example `mkdir()` over plain
HTTP, or any write to a `github:` path) raises `NotImplementedError`. The
per-scheme capabilities are in [Schemes](guides/schemes.md).

## Where to go next

- **[Schemes](guides/schemes.md)**: capability matrix and per-scheme notes
  (backends, timeouts, authentication, security defaults).
- **[CLI](guides/cli.md)**: the `uripath` command.
- **[Extending](guides/extending.md)**: add a path type by subclassing `Path`
  or `UriPath`, and verify it with the bundled contracts.
- **[Divergences from pathlib](divergences.md)**: every deliberate behavioral
  difference from `pathlib.Path`.
- **[API Reference](api/path.md)**: generated from the docstrings.
- **[Changelog](changelog.md)**.
