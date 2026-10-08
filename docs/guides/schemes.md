# Schemes

Every built-in implementation shares one contract: `exists()`, `is_dir()`,
`iterdir()`, `glob()`, `walk()`, `open()`/`read_text()`/`write_bytes()`,
`copy()`/`move()`, `rm()` and `PathSyncer` are derived from a few primitives
(`stat`, `_open`, listing, `_mkdir`, `unlink`, `rmdir`, `rename`, `chmod`).
Where a backend lacks a primitive, the operation raises
`NotImplementedError`; `move()` falls back to copy + delete when `rename()` is
unavailable or the target is elsewhere.

`UriPath("scheme://...")` returns the class registered for the scheme. Every
URI scheme needs the `uri` extra (`uritools`, `netimps`); the extras below
install it too.

## Capability matrix

| Implementation | Read | Write | Append | List | mkdir | Delete | rename | chmod | Extra |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `LocalPath` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | none |
| `MemPath` (no URI scheme) | Yes | Yes | Yes | Yes | Yes | Yes | No | No | none |
| `file:` (`FileUri`) | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | `uri` |
| `data:` (`DataUri`) | Yes | No | No | No | No | No | No | No | `uri` |
| `zip:` (`ZipUri`) | Yes | Local `file:` archive | No | Yes | Local archive | Local archive | Local archive | No | `uri` |
| `tar:` (`TarUri`) | Yes | No | No | Yes | No | No | No | No | `uri` |
| `archive:` / `archive+zip:` / `archive+tar:` | As `zip:` or `tar:` for the detected or pinned format | | | | | | | | `uri` |
| `ftp:` / `ftps:` (`FtpPath`) | Yes | Yes | Yes (`APPE`) | Yes | Yes | Yes | Yes | `SITE CHMOD`, if the server has it | `uri` |
| `http:` / `https:` (`HttpPath`) | Yes | Yes (`PUT`) | Yes (rewrite or `PATCH`) | Yes (HTML index) | No | Yes (`DELETE`) | No | No | `http` |
| `dav:` / `davs:` (`DavPath`) | Yes | Yes | No | Yes (`PROPFIND`) | Yes (`MKCOL`) | Yes | Yes (`MOVE`) | No | `http` |
| `sftp:` (`SftpPath`) | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes | `sftp` or `sftp-async` |
| `s3:` (`S3Path`) | Yes | Yes | No | Yes (prefixes) | Yes (marker) | Yes | Same bucket | No | `s3` |
| `gs:` (`GsPath`) | Yes | Yes | No | Yes (prefixes) | Yes (marker) | Yes | Same bucket | No | `gs` |
| `az:` (`AzPath`) | Yes | Yes | No | Yes (prefixes) | Yes (marker) | Yes | Same container | No | `az` |
| `github:` (`GitHubPath`) | Yes | No | No | Yes | No | No | No | No | `http` |
| `gitlab:` (`GitLabPath`) | Yes | No | No | Yes | No | No | No | No | `http` |
| `git:` / `git+github:` / `git+gitlab:` | As `github:`/`gitlab:` | | | | | | | | `http` |

`stat()` works everywhere. `st_mtime` is real on every implementation except
`data:` and `github:`/`gitlab:`, where it is `0`. `symlink_to()` is
implemented by `LocalPath` and `sftp:` only; `readlink()` by `LocalPath` and
`sftp:`.

## Common behavior

- **Errors** map to pathlib's exception types (`FileNotFoundError`,
  `PermissionError`, `FileExistsError`, `IsADirectoryError`,
  `NotADirectoryError`, `OSError(ENOTEMPTY)`); network timeouts raise
  `TimeoutError`. Transport exceptions are not chained, because their text
  can carry credentials.
- **Credentials** in a URI are redacted from `str()`/`repr()` (the password;
  for `github:`/`gitlab:`/`git:` the whole userinfo). A backend (session,
  connection, token) is reused only for the same scheme, userinfo, host and
  port, so joining a path onto another host never sends it there.
- **`rename()`** stays on one endpoint: a target on another host, bucket,
  container or archive raises `NotImplementedError`, and `move()` copies and
  deletes instead. A `str` target is a path (not URI syntax); a relative one
  is a sibling.
- **Backends** are passed as `UriPath(uri, backend=...)` or
  `path.with_backend(backend)`, and inherited by every derived path. Without
  one, paths derived from one another (`root / name`, `parent`, ...) share the
  one backend the first of them builds, so 20 children of a fresh root use one
  connection; paths constructed separately do not share.

## Local and in-memory

- **`LocalPath`** is `pathlib.WindowsPath`/`PosixPath` with pathlib_next's
  `Path` mixed in.
- **`MemPath`** is a plain `Path` subclass over nested dicts
  (`MemPathBackend`), not a `UriPath`. Share a tree between separately built
  paths with `MemPath(..., backend=other.backend)`. `as_uri()` returns
  `mempath:/...`, but no `mempath:` scheme is registered.
- **`file:`** (`FileUri`) wraps a `LocalPath` (`filepath`) and delegates all
  I/O to it. `rename()` accepts local targets only.
- **`data:`** (`DataUri`, RFC 2397) keeps the whole content in the URI
  (`data:[<mediatype>][;base64],<data>`): a read-only single file with a
  `mediatype` property.

## HTTP and WebDAV

- **`http(s):`** (`HttpPath`) reads with `GET` (uncompressed) and `stat()`s
  with `HEAD`, falling back to `GET` on 405 and following a redirect once, at
  its `Location`; a final URL whose path ends in `/` is a directory. Listing
  parses Apache/nginx-style HTML indexes; a non-HTML
  response raises `NotADirectoryError`, and an HTML file cannot be told apart
  from an index page. An index is read like a file: one that is cut short or
  stalls raises (`OSError` or `TimeoutError`) instead of listing part of it,
  and one larger than `pathlib_next.uri.schemes.http.MAX_LISTING_BYTES` (8 MiB)
  raises `OSError` (`EFBIG`); assign a larger number to list it. Configure it with
  `path.with_session(session, write_method="PUT", append_mode="rewrite",
  **requests_args)`: `requests_args` (`headers=`, `auth=`, `verify=`,
  `timeout=`, ...) go to every request.
  - Writes send `write_method` with the whole body on close. `open("x")`
    calls `stat()` first: a file that exists raises `FileExistsError`, and so
    does any failure of that check other than not-found (nothing is sent).
    The upload carries `If-None-Match: *`, so a server that honours it
    answers 412 (`FileExistsError`) if the file appeared meanwhile.
  - `open("a")`: `append_mode="rewrite"` downloads, appends and re-uploads
    (works on any server, not atomic); `append_mode="patch"` sends `PATCH`
    with a `Content-Range` starting at the size the `HEAD` reply states
    (`OSError` if it has no `Content-Length`) and never falls back.
    A refused `PATCH` raises `PermissionError` (401, 403, 405, 501) or
    `OSError` with the HTTP status (other codes, such as 400). An `open("a")`
    that raises, because reading the current content or the `HEAD` failed,
    has uploaded nothing.
  - `unlink()` refuses a directory; `rmdir()` requires an empty one.
  - Requests time out after `(10, 60)` seconds (connect, read) unless a
    `timeout` is given; `timeout=None` waits forever.
  - Redirects are followed as `requests` follows them for `GET`, `HEAD` and
    `OPTIONS`, but not for `PROPFIND` or the writes: `PROPFIND`, `PUT`,
    `PATCH`, `DELETE`, `MKCOL`, `MOVE` and a custom `write_method` are sent
    with `allow_redirects=False` (an `allow_redirects` in `with_session()`
    does not apply to them). A 307 or 308 (for `PROPFIND` also a 301, 302 or
    303) to the same scheme, host and port is sent once more with the same
    method, headers and body; any other 3xx, a redirect to another origin and
    a second redirect raise `OSError` (`EIO`) naming the status and the
    `Location`. Point the path at the final URL if the server redirects.
  - Credentials in the URL (`https://user:pw@host/`) are sent as Basic
    `auth=`, never inside the request URL, and take priority over `~/.netrc`;
    the octets of the percent-escapes are the credential (`caf%C3%A9` goes out
    as UTF-8 bytes).
  - When a `GET` or `HEAD` is redirected to another host, `requests` drops
    `Authorization` and nothing else: headers other than `Authorization` from
    `with_session(headers=...)` (`X-Api-Key`, a vendor token header) go to the
    new host too. Keep a secret in `Authorization` or `auth=`, or point the
    path at the final URL.
  - The mode of `open()` is matched exactly: `"r+"`, `"w+"` and `"a+"` raise
    `NotImplementedError`.
- **`dav(s):`** (`DavPath`) is WebDAV (RFC 4918) over the equivalent
  `http(s):` URL, with the same `with_session()`. `PROPFIND` gives real
  directory metadata, `MKCOL`/`PUT`/`DELETE`/`MOVE` full writes. A reply that
  is not a `multistatus` document, or that names neither the collection nor
  any member of it, raises `OSError` (`EIO`), and a listing keeps only the
  direct members of the collection it asked about. `unlink()`
  refuses a collection and `rmdir()` checks that it is empty (WebDAV
  `DELETE` is recursive); `rm(recursive=True)` is a single `DELETE`.
  `rename()` does not overwrite an existing target (`FileExistsError`).
  Uploads use `write_method` (default `PUT`) from `with_session()`; append
  mode is not supported, so `append_mode` has no effect.

## FTP

**`ftp(s):`** (`FtpPath`) uses stdlib `ftplib`.

- `FtpBackend(timeout=30.0, ssl_context=None, verify=True)` configures it.
  Sockets time out after 30 seconds by default (`timeout=None` waits forever).
- **`ftps:` verifies the server certificate and host name** before logging in
  and reuses the TLS session on data connections. For a private CA pass
  `FtpBackend(ssl_context=ssl.create_default_context(cafile=...))`;
  `FtpBackend(verify=False)` turns verification off.
- Paths built without `backend=` share one default backend, with one cached
  connection per server and thread, closed when the thread ends. A connection
  idle for more than `IDLE_PROBE_SECONDS` (1 second) is probed with `NOOP`
  before use and replaced if the server dropped it.
- A listing uses `MLSD` (type, size and UTC modification time in one round
  trip) and `stat()` uses `MLST` where the server offers it; without them the
  listing falls back to `NLST` and the stat to `SIZE`/`CWD`.
- Reads download the whole file into memory; writes are buffered in memory
  and uploaded on close. `chmod()` uses `SITE CHMOD` and raises
  `NotImplementedError` when the server lacks it.

## SFTP

**`sftp:`** (`SftpPath`) is a full remote filesystem with two backends:
paramiko (`SftpBackend`, the `sftp` extra) and asyncssh
(`AsyncsshSftpBackend`, the `sftp-async` extra; one shared background event
loop bridges it to the synchronous API).

- **Selection**, highest first: an explicit `backend=` > a
  `SftpPath._default_backend_cls` subclass attribute > the
  `PATHLIB_NEXT_SFTP_BACKEND` environment variable (`auto`, `asyncssh` or
  `paramiko`; naming an uninstalled backend raises `ImportError`) > auto
  (asyncssh if importable, else paramiko).
- **Host keys are verified by default** on both backends, so an unknown or
  changed key fails before any password is sent. paramiko reads
  `~/.ssh/known_hosts` plus ssh_config `UserKnownHostsFile` and rejects
  unknown keys (`RejectPolicy`); asyncssh applies its own `known_hosts` and
  ssh_config handling. The opt-out is explicit, in code:
  `SftpBackend(connect_opts, paramiko.AutoAddPolicy(), known_hosts=None)` or
  `AsyncsshSftpBackend(connect_opts={"known_hosts": None})`.
- **Timeouts**: paramiko's connect, banner, auth and channel-open timeouts
  default to 30 seconds (`SftpBackend(..., timeout=...)`). asyncssh bounds
  each single request at 60 seconds (`AsyncsshSftpBackend(timeout=...)`);
  recursive `copy()`/`rm()` and file transfers have no wall-clock bound.
- **ssh_config**: `SftpPath(url, ssh_config=...)` takes a path, a list of
  paths or `None` (default `~/.ssh/config`). The port is the URI's, else the
  `Port` of the host's config entry, on both backends. The paramiko backend
  expands `Include` (inside a `Host` or `Match` block the included blocks stay
  conditional on it), expands a `ProxyCommand`'s `%h`, `%n`, `%p`, `%r` and
  `%%` with the host, port and user of the connection, and refuses
  `ProxyJump` (use asyncssh, a `ProxyCommand`, or `connect_opts["sock"]`).
- **Host names**: a URI host that is not a plain host name or address (white
  space, quotes, control characters, a leading `-`, shell metacharacters)
  raises `ValueError` on the first connection or config lookup, never a
  `ProxyCommand` argument; the path itself can still be built and joined.
- **Connections** are cached per backend and server (paramiko also per
  thread) and replaced when they drop; `backend.close()` closes them.
- **Capabilities**: `readlink()`/`symlink_to()` on both backends;
  `hardlink_to()` and `chmod(follow_symlinks=False)` on asyncssh only
  (paramiko raises `NotImplementedError` without a round trip). `rename()`
  replaces an existing target where the server supports
  `posix-rename@openssh.com`. On asyncssh, `copy(recursive=True)` to the same
  server and `rm(recursive=True)` run concurrently, bounded by
  `max_concurrency` (default 16); one that raises or is interrupted has
  stopped by the time the exception arrives, and a destination file whose copy
  did not complete is removed. `checksum()` uses the `check-file-handle`
  extension where the server has it (OpenSSH does not).
- `os.fspath()`/`host_fspath()` return the path on the remote host.

## Object storage

**`s3://bucket/key`** (`S3Path`), **`gs://bucket/key`** (`GsPath`) and
**`az://account/container/key`** (`AzPath`) share one model.

- There are no real directories: `is_dir()` is true when any key exists under
  `key/`, `mkdir()` writes a zero-byte `key/` marker, and `rmdir()` requires an
  empty prefix. A key that is both an object and a prefix is the object. A
  trailing `/` in the URI is dropped from the key.
- A prefix that holds keys a directory walk does not reach (an empty, `.` or
  `..` segment, the subtree under a key that is also an object, a `name/`
  key that holds data) cannot be copied, moved or removed recursively: the
  call raises `OSError` naming the keys and changes nothing. A write or
  `rename()` onto a prefix directory raises `IsADirectoryError`.
- Writes are uploaded on close; `open("x")` is a conditional create (an S3
  upload above 5 GiB checks first, then writes); append mode is not
  supported; writes below a missing "directory" succeed.
- `rename()` is a server-side copy + delete within one bucket (container);
  renaming a prefix directory raises `NotImplementedError`, so `move()` copies
  it. `S3Path.rm(recursive=True)` at the bucket root raises
  `PermissionError`.
- Clients: `S3Backend(**client_kwargs)` builds `boto3.client("s3",
  **client_kwargs)`; `GsBackend(**client_kwargs)` builds
  `google.cloud.storage.Client(**client_kwargs)` (for an emulator pass
  `client_options={"api_endpoint": url}` and `use_auth_w_custom_endpoint=False`);
  `AzBackend(account=None, **client_kwargs)` builds a `BlobServiceClient`
  (`connection_string=`, or `account_url=`/`credential=`). An `AzPath`
  without `backend=` targets `https://<account>.blob.core.windows.net` with
  `azure-identity`'s `DefaultAzureCredential` (installed by the `az` extra).

## Archives

**`zip:`/`tar:`** address an entry inside an archive:
`zip:<archive-uri>!/<inner-path>` (the Java-style `!/` separator of JAR URLs).
The `<archive-uri>` is any absolute URI with an explicit scheme, so
`zip:file:///backups/site.zip!/index.html` and
`zip:sftp://host/nightly.zip!/index.html` work the same way. `name`,
`parent`, `glob()`, ... operate on the inner path.

- **Writing** (new members, overwriting, `mkdir()`, `unlink()`, `rmdir()`,
  `rename()`) works for zip archives whose outer URI is a local `file:` path.
  Each change replaces the archive atomically (temporary file, then
  `os.replace`) and keeps the other members' metadata, the archive comment and
  any leading bytes. Every other outer scheme is read-only and is read into
  memory.
- **`tar:`** (also `.tar.gz`/`.tar.bz2`/`.tar.xz`) is read-only.
- **`archive:`** detects the format from the outer name, then from the file's
  magic bytes; `archive+zip:`/`archive+tar:` pin it. All spellings of one
  archive share one open handle.
- An archive inside an archive is addressed by nesting
  (`zip:zip:file:///outer.zip!/inner.zip!/x.txt`) and is read-only; a `!/`
  inside a member name is written `%21/`.
- Member names are normalized as POSIX relative paths, the same way for
  every format: `./x`, `a//b`, `a/./b` and `a/b/../c` all resolve, so a
  member lists and reads under one name however the archive was written
  (`tar -C dir .` and `shutil.make_archive` prefix every member with `./`).
  The spelling as written still works.
- Members whose names would escape the archive (`..` past the root, or an
  absolute path) have no name inside it: never listed, never readable. A
  name that merely a Windows destination would misread -- `C:drive.txt`, or
  one containing `\` -- is a normal member, since both are ordinary
  filenames on POSIX; copying or extracting it onto a Windows path is what
  refuses it. Exception types are the POSIX ones on every platform.

## Git hosting

**`github:`/`gitlab:`** (`<scheme>://host/owner/repo/path/in/repo?ref=<ref>`)
are read-only views of a repository over the REST APIs, using plain
`requests`.

- `ref` (branch, tag or SHA) is optional and carried to every child path.
  Without it, `github:` uses the default branch server-side; `gitlab:` looks
  the default branch up once per backend.
- `host` defaults to `github.com`/`gitlab.com`. Another host is GitHub
  Enterprise (`https://{host}/api/v3`) or self-hosted GitLab
  (`https://{host}/api/v4`); `RepoBackend(api_base=...)` overrides the API
  root. A GitLab project in a subgroup uses GitLab's separator:
  `gitlab://host/group/subgroup/project/-/path`.
- Authentication: `RepoBackend(token=...)`, or the token in the URI userinfo,
  as the password (`github://x-access-token:TOKEN@github.com/owner/repo`) or
  bare (`github://TOKEN@github.com/...`). These schemes redact the whole
  userinfo from `str()`, `repr()`, `as_posix()` and error messages. A token
  sent over plain `http://` to a host other than loopback (an `api_base` you
  set) is still sent, with an `InsecureTransportWarning`.
- Owner, repository and every path segment are percent-encoded into the API
  URL, and a `.` or `..` segment (made with `with_name("..")`, say) raises
  `ValueError` rather than leaving the repository's API root.
- Requests time out after `(10, 60)` seconds (`RepoBackend(timeout=...)`).
  Rate-limit replies raise `OSError(EAGAIN)`.
- GitHub listings come from the contents API (with its type and size) and
  switch to the Git Trees API for directories at its 1,000-entry cap; file
  bodies use the raw media type. GitLab's tree listing has no sizes, so file
  entries are `stat()`ed on demand (a `HEAD` request, no file content).
  Symlinks and submodules read as plain files. Git has no empty directories.
- A reply that is not the shape the API documents (a proxy's HTML page,
  `null`, a truncated body, an array where an object belongs) raises
  `OSError` (`EIO`) naming the path; `exists()` is `False`.
- **`git:`** detects the provider for `github.com` and `gitlab.com` only;
  other hosts raise `ValueError` and need `github:`/`gitlab:` or the pinned
  `git+github:`/`git+gitlab:` forms.

See [Divergences from pathlib](../divergences.md) for the reasons behind these
choices. Every class and backend signature is in the API reference:
[`file:`/`data:`](../api/schemes/local.md),
[`http:`/`dav:`](../api/schemes/http.md), [`ftp:`](../api/schemes/ftp.md),
[`sftp:`](../api/schemes/sftp.md),
[`s3:`/`gs:`/`az:`](../api/schemes/objstore.md),
[archives](../api/schemes/archive.md) and
[`github:`/`gitlab:`/`git:`](../api/schemes/git.md).
