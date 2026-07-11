# Schemes

Capability matrix for every built-in `Path`/`UriPath` implementation. A
"No" means the method raises `NotImplementedError` (or, for `http:`, isn't
meaningful for a read-only scheme) -- everything else (name/suffix parsing,
`glob()`, `walk()`, `copy()`, ...) is derived from these primitives and
works identically across all of them.

| Capability | `LocalPath` | `file:` (`FileUri`) | `mem:` (`MemPath`) | `http(s):` (`HttpPath`) | `sftp:` (`SftpPath`) | `data:` (`DataUri`) | `ftp(s):` (`FtpPath`) | `zip:` (`ZipUri`) | `tar:` (`TarUri`) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Read (`read_text`/`read_bytes`/`open`) | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Write (`write_text`/`write_bytes`/`open("w")`) | Yes | Yes | Yes | No | Yes | No | Yes | New entries only, local outer archive only | No |
| List (`iterdir`) | Yes | Yes | Yes | Yes (scrapes an HTML index) | Yes | No (`NotADirectoryError`) | Yes (MLSD, falls back to NLST) | Yes | Yes |
| Stat (`stat`, `exists`, `is_dir`, `is_file`, ...) | Yes | Yes | Yes | Yes (via `HEAD`, falls back to `GET`) | Yes | Yes (`st_size` from decoded payload) | Yes (MLSD, falls back to SIZE for files) | Yes | Yes |
| `mkdir` | Yes | Yes | Yes | No | Yes | No | Yes | Local outer archive only (zero-length `name/` entry) | No |
| Delete (`unlink`/`rmdir`/`rm`) | Yes | Yes | Yes | No | Yes | No | Yes | No | No |
| `rename` | Yes | Yes | No (`move()` falls back to copy+unlink) | No | Yes | No | Yes | No | No |
| `chmod` | Yes | Yes | No | No | Yes (no `follow_symlinks=False`; paramiko has no `lchmod`) | No | Yes (`SITE CHMOD`, server-dependent) | No | No |
| Extra required | none | none | none | `http` | `sftp` | none (stdlib) | none (stdlib `ftplib`) | none (stdlib `zipfile`) | none (stdlib `tarfile`) |

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
- **`zip:`/`tar:`** address an entry *inside* an archive:
  `zip:<archive-uri>!/<inner-path>` (Java-style `!/` separator, as in JAR
  URLs / NIO `ZipFileSystem`). The `<archive-uri>` half is itself any
  absolute URI with an explicit scheme -- `file:`, `http:`, `sftp:`,
  `ftp:`, even `data:` -- so an archive is readable straight off any other
  backend; `zip:file:///backups/site.zip!/index.html` and
  `zip:sftp://host/nightly.zip!/index.html` both work the same way.
  `segments`/`name`/`parent`/`glob()`/... all operate on the *inner* path.
  Writing a brand-new entry (not overwriting/deleting/renaming an existing
  one -- that would need a full-archive rewrite, not implemented) only
  works when the outer archive is itself a local `file:` URI; every other
  outer scheme is read-only. `tar:` (with transparent `.tar.gz`/`.tar.bz2`/
  `.tar.xz` decompression) is read-only regardless of the outer scheme.
- See [Divergences from pathlib](../divergences.md) for the "explicitly out
  of scope" list (`resolve`, `symlink_to`, `owner`, `expanduser`, ...) that
  applies uniformly across every non-`LocalPath` implementation.
