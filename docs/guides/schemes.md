# Schemes

Capability matrix for every built-in `Path`/`UriPath` implementation. A
"No" means the method raises `NotImplementedError` (or, for `http:`, isn't
meaningful for a read-only scheme) -- everything else (name/suffix parsing,
`glob()`, `walk()`, `copy()`, ...) is derived from these primitives and
works identically across all of them.

| Capability | `LocalPath` | `file:` (`FileUri`) | `mem:` (`MemPath`) | `http(s):` (`HttpPath`) | `sftp:` (`SftpPath`) | `data:` (`DataUri`) | `ftp(s):` (`FtpPath`) | `zip:` (`ZipUri`) | `tar:` (`TarUri`) | `archive:`/`archive+<fmt>:` (`ArchiveUri`) | `dav(s):` (`DavPath`) | `s3:` (`S3Path`) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Read (`read_text`/`read_bytes`/`open`) | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| Write (`write_text`/`write_bytes`/`open("w")`) | Yes | Yes | Yes | Yes (PUT, configurable) | Yes | No | Yes | New/overwritten entries, local outer archive only | No | Same as `zip:`, only if the outer resolves/is detected as zip | Yes | Yes |
| List (`iterdir`) | Yes | Yes | Yes | Yes (scrapes an HTML index) | Yes | No (`NotADirectoryError`) | Yes (MLSD, falls back to NLST) | Yes | Yes | Yes | Yes (PROPFIND) | Yes (prefix + delimiter emulation) |
| Stat (`stat`, `exists`, `is_dir`, `is_file`, ...) | Yes | Yes | Yes | Yes (via `HEAD`, falls back to `GET`) | Yes | Yes (`st_size` from decoded payload) | Yes (MLSD, falls back to SIZE for files) | Yes | Yes | Yes | Yes (PROPFIND) | Yes (`is_dir` is prefix emulation) |
| `mkdir` | Yes | Yes | Yes | No | Yes | No | Yes | Local outer archive only (zero-length `name/` entry) | No | Same as `zip:`, zip-detected only | Yes (MKCOL) | Yes (zero-byte `key/` marker object) |
| Delete (`unlink`/`rmdir`/`rm`) | Yes | Yes | Yes | Yes (DELETE) | Yes | No | Yes | Yes (local outer archive only) | No | Same as `zip:`, zip-detected only | Yes (`rmdir` requires empty, see notes) | Yes (`rmdir` requires empty prefix) |
| `rename` | Yes | Yes | No (`move()` falls back to copy+unlink) | No | Yes | No | Yes | Yes (local outer archive only) | No | Same as `zip:`, zip-detected only | Yes (MOVE) | Yes (server-side `copy_object`+delete, same bucket) |
| `chmod` | Yes | Yes | No | No | Yes (`follow_symlinks=False` works on the asyncssh backend, not paramiko -- see notes) | No | Yes (`SITE CHMOD`, server-dependent) | No | No | No | No | No |
| Extra required | none | none | none | `http` | `sftp` | none (stdlib) | none (stdlib `ftplib`) | none (stdlib `zipfile`) | none (stdlib `tarfile`) | none (reuses `zip:`/`tar:`) | `http` (reused) | `s3` |

Notes:

- **`file:`** is a thin `UriPath` wrapper around `LocalPath` -- it has
  identical capabilities, just addressed by URI instead of a native path.
- **`mem:` (`MemPath`)** isn't a `UriPath` at all -- it's a plain `Path`
  subclass (Track A of [Extending](extending.md)), backed by nested dicts
  (`MemPathBackend`). No `as_uri()` scheme is registered for it; construct
  it directly via `MemPath(...)`.
- **`http(s):`** supports writing via `PUT` (default, configurable to `POST` or other verbs via `with_session(..., write_method=...)`) and deleting via `DELETE` (where `rmdir()` checks empty status first). Directory listing parses Apache/nginx-style HTML indexes using a fast, zero-dependency parser.
- **`sftp:`** has the fullest capability set of the URI schemes (it's a
  real remote filesystem protocol), and is the one scheme with **two
  selectable backends**: paramiko (sync, the `sftp` extra) and asyncssh
  (async internally, bridged to a sync API via one shared background
  event loop, the `sftp-async` extra -- also works on Python 3.9 via a
  version-pinned release, `asyncssh<2.22`). Selection precedence, highest
  to lowest: an explicit `backend=` constructor kwarg > a
  `SftpPath._default_backend_cls` subclass override > the
  `PATHLIB_NEXT_SFTP_BACKEND` env var (`"paramiko"`/`"asyncssh"`/`"auto"`,
  default `"auto"`: asyncssh if importable, else paramiko) > auto-detect.
  `PATHLIB_NEXT_SFTP_BACKEND=asyncssh` with the package not installed
  raises immediately rather than silently falling back to paramiko. The
  asyncssh backend caches connections per `(backend, source)` (no
  thread dimension needed, unlike paramiko's `(backend, source, thread)`
  -- one shared connection serves concurrent calls from any calling
  thread). Both backends implement `readlink()`/`symlink_to()`
  (core SFTPv3 operations); `hardlink_to()` and
  `chmod(follow_symlinks=False)` work on the asyncssh backend only
  (paramiko's `SFTPClient` has no hard-link or `lchmod` equivalent at
  all -- both raise `NotImplementedError` immediately, no server round
  trip). See `pathlib_next.uri.schemes.sftp`.
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
  Writing (new entries, overwriting/deleting/renaming existing ones -- the
  latter three via a full-archive rewrite to a temp file, then an atomic
  `os.replace`) only works when the outer archive is itself a local `file:`
  URI; every other outer scheme is read-only. `tar:` (with transparent
  `.tar.gz`/`.tar.bz2`/`.tar.xz` decompression) is read-only regardless of
  the outer scheme.
- **`archive:`/`archive+<fmt>:`** is a convenience catch-all over `zip:`/
  `tar:`: bare `archive:<archive-uri>!/<inner-path>` auto-detects the
  format (outer filename extension first, then a `PK`-header magic-byte
  sniff), while `archive+zip:`/`archive+tar:` pin the format explicitly and
  skip detection. Resolves to the same shared backend (and registry key) as
  the dedicated `zip:`/`tar:` schemes -- `archive:...!/x` and `zip:...!/x`
  on the same outer archive share one open handle. `zip:`/`tar:` remain the
  primary, explicit schemes; `archive:` exists for callers that don't know
  (or don't care about) the format ahead of time.
- **`dav(s):`** is WebDAV (RFC 4918) layered on `HttpPath` -- PROPFIND
  replaces HTML-index scraping for real stat/listdir metadata, and PUT/
  DELETE/MKCOL/MOVE give it full write support (unlike plain `http(s):`).
  Requests go out over the equivalent `http:`/`https:` URL; `as_uri()`
  still reports `dav:`/`davs:`. Reuses the `http` extra, no new dependency.
  `rmdir()` enforces pathlib's "must be empty" contract with a depth-1
  PROPFIND before issuing `DELETE`; the native recursive `DELETE` (RFC
  4918) is still available, and cheaper than a client-side walk, via
  `rm(recursive=True)`.
- **`s3:`** (`s3://bucket/key/path`) has no real directories: `is_dir()`
  is prefix emulation (any object key under `"<path>/"`), and `mkdir()`
  creates a zero-byte `"<path>/"` marker object (the same convention the
  AWS console itself uses for an empty "folder") -- `rmdir()` requires no
  other keys under that prefix (pathlib's "must be empty" semantics, same
  as `dav:`'s `rmdir()` above). A single `boto3` client is cached per
  backend (documented thread-safe, unlike `sftp:`/`ftp:`'s per-thread
  connection pools).
- See [Divergences from pathlib](../divergences.md) for the "explicitly out
  of scope" list (`resolve`, `symlink_to`, `owner`, `expanduser`, ...) that
  applies uniformly across every non-`LocalPath` implementation.
