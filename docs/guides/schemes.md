# Schemes

Capability matrix for every built-in `Path`/`UriPath` implementation. A
"No" means the method raises `NotImplementedError` (or, for `http:`, isn't
meaningful for a read-only scheme) -- everything else (name/suffix parsing,
`glob()`, `walk()`, `copy()`, ...) is derived from these primitives and
works identically across all of them.

| Capability | `LocalPath` | `file:` (`FileUri`) | `mem:` (`MemPath`) | `http(s):` (`HttpPath`) | `sftp:` (`SftpPath`) |
| --- | --- | --- | --- | --- | --- |
| Read (`read_text`/`read_bytes`/`open`) | Yes | Yes | Yes | Yes | Yes |
| Write (`write_text`/`write_bytes`/`open("w")`) | Yes | Yes | Yes | No | Yes |
| List (`iterdir`) | Yes | Yes | Yes | Yes (scrapes an HTML index) | Yes |
| Stat (`stat`, `exists`, `is_dir`, `is_file`, ...) | Yes | Yes | Yes | Yes (via `HEAD`, falls back to `GET`) | Yes |
| `mkdir` | Yes | Yes | Yes | No | Yes |
| Delete (`unlink`/`rmdir`/`rm`) | Yes | Yes | Yes | No | Yes |
| `rename` | Yes | Yes | No (`move()` falls back to copy+unlink) | No | Yes |
| `chmod` | Yes | Yes | No | No | Yes (no `follow_symlinks=False`; paramiko has no `lchmod`) |
| Extra required | none | none | none | `http` | `sftp` |

Notes:

- **`file:`** is a thin `UriPath` wrapper around `LocalPath` -- it has
  identical capabilities, just addressed by URI instead of a native path.
- **`mem:` (`MemPath`)** isn't a `UriPath` at all -- it's a plain `Path`
  subclass (Track A of [Extending](extending.md)), backed by nested dicts
  (`MemPathBackend`). No `as_uri()` scheme is registered for it; construct
  it directly via `MemPath(...)`.
- **`http(s):`** is inherently read-only: there's no portable "create/
  delete/rename a resource" over plain HTTP. Directory listing depends on
  the server producing an Apache/nginx-style HTML index that
  `htmllistparse` can parse (confirmed against Python's own
  `http.server.SimpleHTTPRequestHandler`).
- **`sftp:`** has the fullest capability set of the URI schemes (it's a
  real remote filesystem protocol). Connections are cached per
  `(backend, source, thread)` (see `pathlib_next.uri.schemes.sftp`).
- See [Divergences from pathlib](../divergences.md) for the "explicitly out
  of scope" list (`resolve`, `symlink_to`, `owner`, `expanduser`, ...) that
  applies uniformly across every non-`LocalPath` implementation.
