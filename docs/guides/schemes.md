# Schemes

Capability matrix for every built-in `Path`/`UriPath` implementation. A
"No" means the method raises `NotImplementedError` (or, for `http:`, isn't
meaningful for a read-only scheme) -- everything else (name/suffix parsing,
`glob()`, `walk()`, `copy()`, ...) is derived from these primitives and
works identically across all of them.

| Capability | `LocalPath` | `file:` (`FileUri`) | `mem:` (`MemPath`) | `http(s):` (`HttpPath`) | `sftp:` (`SftpPath`) | `data:` (`DataUri`) | `ftp(s):` (`FtpPath`) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Read (`read_text`/`read_bytes`/`open`) | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Write (`write_text`/`write_bytes`/`open("w")`) | Yes | Yes | Yes | No | Yes | No | Yes |
| List (`iterdir`) | Yes | Yes | Yes | Yes (scrapes an HTML index) | Yes | No (`NotADirectoryError`) | Yes (MLSD, falls back to NLST) |
| Stat (`stat`, `exists`, `is_dir`, `is_file`, ...) | Yes | Yes | Yes | Yes (via `HEAD`, falls back to `GET`) | Yes | Yes (`st_size` from decoded payload) | Yes (MLSD, falls back to SIZE for files) |
| `mkdir` | Yes | Yes | Yes | No | Yes | No | Yes |
| Delete (`unlink`/`rmdir`/`rm`) | Yes | Yes | Yes | No | Yes | No | Yes |
| `rename` | Yes | Yes | No (`move()` falls back to copy+unlink) | No | Yes | No | Yes |
| `chmod` | Yes | Yes | No | No | Yes (no `follow_symlinks=False`; paramiko has no `lchmod`) | No | Yes (`SITE CHMOD`, server-dependent) |
| Extra required | none | none | none | `http` | `sftp` | none (stdlib) | none (stdlib `ftplib`) |

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
- **`data:`** (RFC 2397) has no server or connection at all -- the entire
  "file" content lives in the URI string itself
  (`data:[<mediatype>][;base64],<data>`). It's always a single file, never a
  directory.
- **`ftp(s):`** uses the same connection-cache pattern as `sftp:` (stdlib
  `ftplib`, no extra required). Prefers MLSD (RFC 3659) for listing/stat --
  gives type/size/modify in one round trip -- and falls back to NLST/SIZE on
  servers that don't support it (that fallback path can't distinguish "file
  doesn't exist" from "is a directory" for `stat()`, since SIZE only works on
  files).
- See [Divergences from pathlib](../divergences.md) for the "explicitly out
  of scope" list (`resolve`, `symlink_to`, `owner`, `expanduser`, ...) that
  applies uniformly across every non-`LocalPath` implementation.
