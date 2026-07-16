# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.8.1] - 2026-07-16

### Fixed
- **`LocalPath.walk()`/`rm()` raised `TypeError: cannot unpack non-iterable
  DirEntry object` on Python 3.11/3.12.** Those stdlib versions define their
  own `pathlib.Path._scandir()` (returning raw `os.scandir()` `DirEntry`
  objects), which sits ahead of this project's `_scandir()` in `LocalPath`'s
  MRO and silently shadowed it -- breaking the `(name, FileStat|None)`
  contract `walk()`/`glob()`/`rm()` expect. `LocalPath` now defines its own
  `_scandir()` explicitly, reusing each `DirEntry`'s cached `lstat()` so the
  perf win from `_scandir()` unification is preserved. On 3.12+, stdlib
  `pathlib.Path` also defines its own `walk()` ahead of ours in the MRO, and
  that stdlib `walk()` treats `self._scandir()`'s return value as a context
  manager (`with scandir_it:`) -- our own `_scandir()` is a plain generator,
  so stdlib's `walk()` raised `TypeError: 'generator' object does not
  support the context manager protocol` even with the override above.
  `LocalPath` now also overrides `walk()` explicitly, routing to this
  project's own implementation regardless of Python version. Introduced in
  0.8.0 (`8cdbefa`), exposed on the CI 3.11/3.12 legs.
- **`Test No-Extras` CI job was red.** `tests/test_smoke.py` unconditionally
  constructed an `http://`/`sftp://` `UriPath` in two tests, requiring
  `requests`/`paramiko` even though the no-extras job installs neither; a
  third test wrongly assumed `S3Path` requires `boto3` to register (it only
  needs `botocore`, imported lazily inside a method). The two hard tests now
  `pytest.importorskip` their extra; the `S3Path` check now probes for
  `botocore`. Introduced in 0.8.0 (`94bd545`/`8cdbefa`), fixed with the
  expected skip count (2) verified in a real no-extras venv.
- **Importable on a clean Python 3.9 install.** `pathlib_next.utils` used
  `typing.ParamSpec` (3.10+), falling back to `typing_extensions.ParamSpec` and
  then to a bare `typing.TypeVar`. A `TypeVar` has no `.args`, so the
  `*args: K.args` annotations raised `AttributeError: 'TypeVar' object has no
  attribute 'args'` at import time, making `import pathlib_next` fail on 3.9
  whenever `typing_extensions` was absent. Since `typing_extensions` is not a
  runtime dependency, this broke a plain `pip install pathlib_next` on 3.9. The
  final fallback is now a minimal `ParamSpec` shim providing `.args`/`.kwargs`,
  so no runtime dependency is added and 3.10+ keeps using `typing.ParamSpec`
  unchanged.

## [0.8.0] - 2026-07-13

### Added
- `uripath` command-line tool (`pathlib_next.tools.uripath`) for reading,
  writing, copying, removing, and syncing local or URI-backed paths. `-`
  works as stdin/stdout for byte-stream operations.
- Recursive benchmark probes for local, memory, object-store, and SFTP
  backends, including provider call-shape rows for recursive deletes.
- Provider-native recursive delete overrides for `S3Path`, `GsPath`, and
  `AzPath`, with bucket/container-root guards.
- `git:` convenience dispatch over the existing `github:`/`gitlab:` providers, plus explicit `git+github:` and `git+gitlab:` forms for self-hosted or enterprise instances. `git:` only auto-detects public `github.com`/`gitlab.com`; ambiguous hosts now raise a clear `ValueError` naming the explicit alternatives.
- HTTP write support (`PUT`, customizable to `POST` or other verbs via `write_method` configuration or `with_session()`) for `HttpPath`.
- HTTP delete support (`DELETE` for `unlink()` and `rmdir()`) for `HttpPath`.
- Comprehensive HTTP exception mapping in `HttpPath` translating client/server/timeout/connection errors into standard built-in `OSError` subclasses (`FileNotFoundError`, `PermissionError`, `FileExistsError`, `TimeoutError`, `ConnectionError`, `NotImplementedError`, or generic `OSError`).
- Dynamic loading of custom URI scheme plugins via standard Python packaging entry points under the `"pathlib_next.schemes"` group, allowing third-party package extensibility.
- Lazy-loading for all builtin scheme implementations (s3, sftp, http, etc.) to significantly reduce start-up and import overhead when heavy libraries are not needed.
- MD5 and SHA-256 checksum helpers in `pathlib_next.utils` (`md5` and `sha256`).
- Optional `checksum` parameter in `PathSyncer`, defaulting to the new `md5` helper.
- Recursive directory copying via `Path.copy(recursive=True)`.
- Support for recursive folder moves falling back to recursive copy + recursive delete when `rename` is not supported.
- Archive utilities `make_archive` and `unpack_archive` supporting ZIP and TAR formats using memory-efficient chunk streaming.
- Hierarchical test contracts: `PurePathContract` (pure path operations) and `ReadPathContract` (read-only path operations), allowing contract-based verification of read-only and memory/archive paths.
- Contract test suites wired for `DataUri`, `ZipUri`, `TarUri`, and `HttpPath`.
- Dedicated unit tests for `Path.walk()`, `samefile()`, and `Stat` device queries.
- Comprehensive runnable examples in `examples/` for URI schemes: offline (`data_and_archive.py`) and environment-variable configured ones (`ftp_listing.py`, `webdav_roundtrip.py`, `s3_listing.py`).
- Split monolith API reference documentation into per-module pages (`path`, `uri`, `mempath`, `utils`, `testing`).
- Complete docstring coverage for all public methods/properties across `Pathname`, `Path`, `Uri`, `UriPath`, and protocols, and configured `mkdocs` to enforce docstring presence (`show_if_no_docstring: false`).
- Detailed documentation of contract testing levels (`PurePathContract`, `ReadPathContract`, `PathContract`) in the extending guide.
- In-process real-server contract tests for `FtpPath` (pyftpdlib), `DavPath` (wsgidav/cheroot), and `S3Path` (moto mock_aws): `TestFtpContract`, `TestDavContract`, `TestS3Contract` run the full `PathContract` suite against live local servers.
- `ftp_server`, `dav_server`, and `s3_server` pytest fixtures in `conftest.py` serving ephemeral in-process servers with pre-populated `fixture_tree` contents.
- Entry-point declarations in `pyproject.toml` (`pathlib_next.schemes` group) for all built-in schemes, enabling pip-installed external packages to auto-register custom schemes.
- Plugin discovery tests in `tests/test_plugins.py` covering `_load_entry_point`, `_load_builtin_scheme`, and `get_scheme_cls` integration.
- Property-based tests (`hypothesis`, new `dev` extra) in `tests/test_properties.py`: URI parse/format round-trip identity, join associativity, and parity with `pathlib.PurePosixPath` for `segments`/`name`/`relative_to`/`match`/`is_relative_to`.
- `ZipUri`/`ArchiveUri` archive handle registry: independently-constructed `UriPath("zip:...")`/`"tar:..."` instances pointing at the same outer archive now share one `_ArchiveBackend` (keyed by backend class + outer URI, a `weakref.WeakValueDictionary`) instead of each opening its own handle -- fixes stale reads and out-of-sync writes across separately-constructed instances. The backend closes its handle automatically (`__del__`) once every referencing path is garbage-collected.
- Full `ZipUri` write support: `unlink()`, `rmdir()` (empty-dir check, mirrors `S3Path.rmdir()`), `rename()` (renames a directory's nested entries too), and overwriting an existing entry's content (previously `open("w")` on an existing entry silently appended a duplicate zipfile entry instead of replacing it). All four go through a new safe full-archive rewrite (`_ZipBackend._rewrite`) since `zipfile` has no in-place entry mutation: writes to a temp file beside the outer archive, then atomically replaces it (`os.replace`). Requires a local (`file:`) outer archive, same as existing new-entry writes.
- `archive:` catch-all URI scheme: auto-detects zip vs. tar for the outer archive (filename extension first, then a magic-byte sniff shared with `unpack_archive` via the new `utils.archive._detect_format` helper) instead of requiring the caller to know the format up front. Explicit `archive+zip:`/`archive+tar:` forms skip detection outright. `archive:...!/x` and `zip:...!/x` pointing at the same outer archive share one backend (same registry as `zip:`/`tar:`), and write support (new/overwritten entries, `unlink`/`rmdir`/`rename`) works through `archive:` exactly as it does through `zip:` when the detected format is zip and the outer archive is local -- tar-detected instances correctly raise `NotImplementedError` on any write attempt.
- New `gs:` (Google Cloud Storage) and `az:` (Azure Blob Storage) URI schemes (`GsPath`/`AzPath` in `pathlib_next.uri.schemes.gs`/`.az`, with `GsBackend`/`AzBackend` for credential/endpoint override): `gs://bucket/key/path` and `az://account/container/key/path`. Both support full `PathContract` (read/write/list/delete/rename), reusing the prefix-emulation directory semantics of `S3Path` (no real directories; `is_dir()` checks for keys under `"<path>/"`, `mkdir()` writes a zero-byte `"<path>/"` marker, `rmdir()` requires empty). Wired to `PathContract` against faithful in-process fake JSON/XML REST API servers (`gcs_api_server`/`gs_server`, `az_api_server`/`az_server` in `conftest.py`), plus scheme-specific unit tests. New `examples/gs_listing.py`/`examples/az_listing.py` (env-var gated, fail-soft). Like `S3Path`, both cache one service client per backend instance (thread-safe for both SDKs). Report no mtime (`st_mtime=0`, documented divergence). Each is its own `pyproject.toml` extra: `gs` (`google-cloud-storage`) and `az` (`azure-storage-blob`). `rename()` uses server-side copy+delete (same bucket/container only) instead of the generic download+upload+delete `move()` fallback.
- New `github:`/`gitlab:` read-only URI schemes (`GitHubPath` in `pathlib_next.uri.schemes.github`, `GitLabPath` in `pathlib_next.uri.schemes.gitlab`, sharing a private `_RepoApiPath` base and a plain-`requests` `RepoBackend`, no PyGithub/python-gitlab SDK): `<scheme>://host/owner/repo/path/in/repo?ref=<ref>`, `ref` always optional in the query string. `GitHubPath` lists via the contents API (one call gives type/size for a whole directory) and reads file bodies via the `raw` media type; `GitLabPath` lists via the tree API (no size, so only directory entries get a stat hint) and reads via the files `/raw` endpoint, resolving+caching the project's default branch itself when `ref` is omitted (GitLab's file endpoints -- unlike its tree endpoint -- 400 if `ref` is missing, confirmed live against gitlab.com). `host` defaults to the public SaaS host; any other host is treated as GitHub Enterprise (`https://{host}/api/v3`) or a self-hosted GitLab (`https://{host}/api/v4`). Auth via a bearer token (`RepoBackend(token=...)`) or URI userinfo. Both reuse the `http` extra (no new extra added). Wired to `ReadPathContract` against faithful in-process fake API servers (`github_api_server`/`gitlab_api_server` in `conftest.py`), plus scheme-specific unit tests (ref propagation through `iterdir()`, rate-limit/error translation, GitHub Enterprise API-base derivation, GitLab dir-vs-file stat disambiguation). New `examples/github_listing.py`/`examples/gitlab_listing.py`.
- New `sftp-async` extra: an `AsyncsshSftpBackend` (`asyncssh`, async internally, bridged to a sync API through one shared background event loop) alongside the existing paramiko-based `SftpBackend`. Auto-selected when `asyncssh` is importable (paramiko remains the fallback); override via the `PATHLIB_NEXT_SFTP_BACKEND` env var (`"paramiko"`/`"asyncssh"`/`"auto"`) or a `SftpPath._default_backend_cls` subclass hook -- precedence, highest to lowest: explicit `backend=` kwarg > `_default_backend_cls` > env var > auto-detect. `PATHLIB_NEXT_SFTP_BACKEND=asyncssh` with the package missing raises immediately rather than silently falling back. Connections are cached per `(backend, source)` (no thread dimension needed -- one shared connection serves concurrent calls from any calling thread, unlike paramiko's `(backend, source, thread)` cache). Works on Python 3.9 too via a verified version pin (`asyncssh<2.22`; current `asyncssh` needs >=3.10) resolved automatically through `pyproject.toml` environment markers -- no code branching. New `SftpPath.symlink_to()`/`readlink()` (both backends -- core SFTPv3 operations) and `hardlink_to()` (asyncssh backend only; paramiko's `SFTPClient` has no hard-link operation, so it raises `NotImplementedError` immediately with no server round trip). `chmod(follow_symlinks=False)` now works on the asyncssh backend (native support) while still raising `NotImplementedError` on paramiko (no `lchmod` equivalent). This is additive, not a performance change -- the (separate, unscheduled) concurrent-fan-out work that would actually exploit asyncssh's pipelining remains future work.

### Changed
- Recursive `Path.rm()` now deletes bottom-up using non-following listing
  metadata where available, avoiding traversal through directory symlinks
  and reducing extra stat calls for metadata-rich backends.
- Asyncssh SFTP recursive copy/remove now use native bounded async helpers
  for ordinary files/directories instead of recursing through sync path
  methods on the bridge loop.
- `PathSyncer` reuses child metadata during tree sync when that metadata is
  consistent with the active symlink-following policy.
- Replaced the third-party `htmllistparse` and `bs4` directory listing scraper dependencies with a hand-rolled, zero-dependency `html.parser.HTMLParser` subclass (`_DirectoryListingParser`), dropping both from the `http` extra in `pyproject.toml`. Verified equivalent output (name/size/modified, both Apache-`<pre>` and nginx-`<table>` formats) against the replaced `bs4`+`html5lib`+`htmllistparse` implementation, and 2.9x-6.2x faster depending on format/listing size (`benchmarks/bench.py`'s `8`/`9` entries benchmark the new parser alone going forward, since the old implementation no longer exists in the tree).
- Matrix expansion in GitHub Actions CI to test Python 3.10, 3.11, and 3.12 (on Ubuntu).
- Added a "no-extras" CI job to run tests without optional dependencies installed.
- `PathSyncer.log()` now logs through `logging.getLogger("pathlib_next.sync")`
  at `INFO` instead of calling `print()` -- stdout consumers must configure
  logging (e.g. `logging.basicConfig()`) to see sync progress again.
  `EVENT_LOG_FORMAT` switched from `str.format` (`{event}`) to `%`-style
  placeholders to match, and `log()` remains overridable for custom routing.
- `SyncEvent` members are now numbered sequentially (previously a mix of
  explicit ints and `enum.auto()`, which raised a `DeprecationWarning` on
  Python 3.13). Values are not part of any documented/persisted contract.
- Optimized performance across pure paths and URIs:
  - Cache `Uri.segments` in a slot to avoid re-splitting the path string on every access.
  - Cache `Uri.suffix` and `Uri.stem` in slots.
  - Optimize `Source.__bool__` to use lazy index accesses and avoid tuple iteration.
  - Short-circuit `Query.__new__` when the input is already a matching `Query` instance.
  - `Uri._parse_uri()`/`Source.from_str()`: one-pass component extraction from `uritools.urisplit()`'s raw fields instead of calling its seven `get*()` accessors, each of which independently re-`rpartition`s the authority string and re-decodes. Ported (not reinvented) from `uritools.SplitResult`'s own property/getter logic -- including one of its quirks, reproduced on purpose (see `uri/source.py::_split_authority`) -- and verified equivalent by fuzzing 20,000+ generated URIs against uritools as the oracle (`tests/test_properties.py`, which stays the enforcement mechanism, not just a one-time check). `Uri._format_parsed_parts()`/`DavPath._wire_uri()`: direct string assembly instead of `uritools.uricompose()`'s full re-validation, for the same reason and with the same fuzzing rigor (`uri/source.py::_compose_uri`) -- both bypass a general-purpose library's necessarily-defensive validation only where the input is already known-canonical (parsed or otherwise internally normalized), not for arbitrary/untrusted URIs. `uritools` itself is unchanged as a dependency and remains the parsing/composing engine underneath both fast paths -- a hand-rolled RFC 3986 implementation was evaluated and rejected (verdict: slower or not worth the permanent edge-case-ownership cost). Measured on `.venv/3.12.10`, unique URIs per iteration (a repeated-URI microbenchmark flatters by masking real per-call cost): the full `Uri(unique_url).as_uri()` round trip (parse + compose, both changes) is ~20-25% faster; the parse side alone, isolated from `Uri.__new__`'s slot-initialization overhead (unaffected by this work), is ~17-30% faster on its own (see `benchmarks/bench.py`'s `1b`/`1c` entries).
- New `Path._scandir()` / `UriPath._scandir()` protocol: schemes whose
  listing call already returns type/size/mtime for every child (HTML
  directory index, WebDAV PROPFIND, SFTP `listdir_attr`, FTP MLSD, an S3
  `list_objects_v2` page) can now yield `(name, FileStat)` pairs directly,
  and `walk()`/`glob()` answer `is_dir()` from that instead of a `stat()`
  round trip per entry -- a remote-tree walk goes from O(entries) requests
  to O(dirs). `HttpPath`, `DavPath`, `SftpPath`, `FtpPath`, and `S3Path` all
  adopt it; `_listdir()`/`iterdir()` remain fully supported for schemes that
  don't override `_scandir()` (no behavior change, no win). On the local
  `http_server` benchmark fixture, HTTP glob/walk over the fixture tree are
  ~89-94% faster than the already-optimized pre-`_scandir()` baseline (see
  `benchmarks/bench.py`). `HttpPath` also drops its `_isdir` instance-cache
  slot and its `is_dir()`/`is_file()` overrides (now derived generically
  from `stat()`, like every other scheme) in favor of a single-use stat
  hint seeded by `_scandir()`; `DavPath`'s now-redundant `iterdir()`/
  `is_dir()`/`is_file()` overrides are removed for the same reason.
- **Breaking** (pre-1.0, no compat shim kept): `uri/schemes/` module naming convention
  -- every module is now named after the main URI scheme it implements (TLS/secondary
  variants live with their main scheme). `webdav.py` -> `dav.py`; import from
  `pathlib_next.uri.schemes.dav` (the old `pathlib_next.uri.schemes.webdav` path no
  longer exists). `archive.py` -> `archive/` package (`_base.py` shared machinery,
  `zip.py`, `tar.py`) -- import-compatible for free, `pathlib_next.uri.schemes.archive`
  still resolves (now the package) and re-exports `ArchiveUri`/`ZipUri`/`TarUri`.
  `sftp.py` -> `sftp/` package (`_paramiko.py` holds the existing paramiko-backed
  `SftpBackend`; `__init__.py` keeps `SftpPath`/`BaseSftpBackend`) -- same free
  import-compat, `pathlib_next.uri.schemes.sftp` still resolves and re-exports
  `SftpPath`/`BaseSftpBackend`/`SftpBackend`. Prepares the layout for an upcoming
  second (asyncssh) backend; `SftpBackend` gained a `default()` classmethod factory
  so `SftpPath._initbackend()` doesn't need to import `paramiko` itself.
- `SftpPath`'s connection caching moved from an external cache wrapping `backend.client()`
  calls to being each backend's own responsibility (`SftpPath._sftpclient` is now a
  trivial `self.backend.client(self.source)`, no per-backend branching). Needed so the
  new asyncssh backend can use its own `(backend, source)`-keyed cache (see the
  `sftp-async` entry above) without `SftpPath` needing to know which caching scheme
  applies. **Behavior-affecting for custom `BaseSftpBackend` subclasses**: a `client()`
  override that doesn't cache internally will now be called on every `_sftpclient`
  access, not just on a cache miss -- `SftpBackend`/`AsyncsshSftpBackend` both cache
  internally, so this only matters for third-party/test-double backends.
- `TestSftpContract`'s in-process test server (`tests/conftest.py::sftp_server`) is now
  asyncssh's own `SFTPServer` (chrooted to `fixture_tree`) instead of a ~150-line
  hand-rolled paramiko `ServerInterface`/`SFTPServerInterface` -- a client backend choice
  is independent of which library the test server uses (verified: a paramiko client
  talks standard SFTP to an asyncssh server fine). `TestSftpContract` itself is now
  parametrized across both client backends (`paramiko`, `asyncssh`).

### Fixed
- Recursive delete on exact object-store keys now treats the exact object as
  the addressed path before considering a `"<key>/"` prefix tree, preventing
  accidental prefix-tree deletion for `S3Path`, `GsPath`, and `AzPath`.
- Azure recursive delete falls back from `delete_blobs()` to per-blob
  deletion when a provider or emulator rejects the batch API.
- `Uri.relative_to()` computed the remaining segments from the raw `.segments` property instead of the root-aware `_segments_of()` helper `is_relative_to()` already used -- `Uri("/").segments` is the 2-tuple `("", "")` (an artifact of `"/".split("/")`), so `relative_to(<root>)` silently dropped the child's only real segment (e.g. `Uri("/a").relative_to(Uri("/"))` produced `""` instead of `"a"`). Found by the new property-based test suite.
- `TestSftpContract`'s in-process paramiko test server (`tests/conftest.py::sftp_server`) deadlocked every real I/O test: its `_SSHServer.check_channel_subsystem_request()` override returned `name == "sftp"` directly instead of delegating to `paramiko.ServerInterface`'s default implementation, which is what actually instantiates and starts the registered `SFTPServer` handler thread (`handler.start()`). Without it, the channel was reported "hooked up" to the client but nothing server-side ever read from or responded on it, so `SFTPClient.from_transport()` blocked forever in version negotiation. Fixed by removing the override (the inherited default already does exactly what the removed comment claimed it did).
- `SftpPath._mkdir()`/`_open(mode="x")` propagated a generic, untyped `OSError("Failure")` when the target already existed -- SFTPv3 has no dedicated "already exists" status code, so paramiko's server-side `convert_errno()` falls through to `SFTP_FAILURE` for `EEXIST` (unlike `ENOENT`, which it does map, giving a proper `FileNotFoundError`). Both now check `self.exists()` on failure and raise `FileExistsError` to match every other scheme's `mkdir`/`touch(exist_ok=False)` contract (mirrors `FtpPath._mkdir()`'s existing check-after-failure pattern). Found by `TestSftpContract` once the deadlock above was fixed and it could actually run.
- `FtpPath.stat()` returned `FileNotFoundError` for the FTP root path `"/"` because `_mlsd_entry()` has no parent directory to query; now uses `CWD /` to confirm the root exists as a directory.
- `FtpPath.rmdir()` propagated raw `ftplib.error_perm` (550) instead of `OSError` when the directory was non-empty, violating the pathlib contract.
- `FtpPath.chmod()` raised `ftplib.error_perm` when the server rejected `SITE CHMOD` (pyftpdlib does not implement it); now converts to `NotImplementedError` so `Path.copy()` silently skips the metadata step.
- `pytest filterwarnings` updated to suppress `boto3.exceptions.PythonDeprecationWarning` (boto3 EOL notice for Python 3.9, inherits `Warning` not `DeprecationWarning`) and `ResourceWarning` from daemon-thread server socket cleanup at GC teardown.
- README and docs landing page were still describing the pre-0.6.0 scheme set:
  the capability matrix, extras table, and quick starts now cover `data:`,
  `ftp(s):`, `zip:`/`tar:`, `dav(s):`, and `s3:` (all shipped in 0.6.0/0.7.0
  but previously only documented in the Schemes guide).
- `LRU.maxsize` setter raised `TypeError` when shrinking below the current
  fill (`OrderedDict.pop()` was called with the `last=False` kwarg meant for
  `popitem()`).
- `DavPath.rmdir()` mapped directly to WebDAV `DELETE`, which is recursive
  by spec (RFC 4918) -- it silently deleted non-empty collections instead
  of enforcing pathlib's "must be empty" contract like every other scheme.
  Now does a depth-1 PROPFIND first and raises `OSError` (`ENOTEMPTY`) if
  children exist. The native recursive `DELETE` is still available, and
  cheaper than the base class's client-side walk, via the new
  `DavPath.rm(recursive=True)` override (one request).
- `Uri._make_child_relpath()` doubled the join slash for any scheme whose
  `path` already ends in "/" (e.g. `f"{self.path}/{name}"` on an HTTP/DAV
  directory path produced `"//name"`); also now treats an empty path with
  an authority present as the same root as `"/"` (RFC 3986:
  `"http://host"` == `"http://host/"`) instead of joining a bare, ambiguous
  name with no leading slash.
- `_DirectoryListingParser._RE_FILESIZE`'s digit class excluded `,` -- the
  `<table>` path strips commas from cell text before matching, but the
  `<pre>` path matches first, so a comma-thousands size like `1,024`
  matched only `"1"`, truncating `size` and leaking `,024` into
  `description`.
- The RFC-1123 datetime bucket's trailing timezone match
  (`... \d{2}:\d{2}:\d{2} .+`) used an unbounded, greedy `.+` that
  swallowed the rest of the `<pre>` listing line, including any trailing
  size/description text on the same row -- `time.strptime()` then raised
  on the unconverted data, silently dropping `modified` **and** every
  field after it for that entry. Narrowed to `\S+` (the timezone is one
  token).
- `_DirectoryListingParser`'s absolute-href filter was a blanket
  `startswith('/')` -- a reverse-proxied/absolute-URL-configured server
  rendering *every* entry (not just the parent-directory link) as an
  absolute href got back a completely empty listing, with no fallback
  able to recover it. Scoped the filter to hrefs outside the listing's own
  directory (parsed from `<title>Index of ...</title>`) instead, falling
  back to the old blanket-drop behavior only when no title was parseable.
- `HttpPath.stat()`'s post-redirect HEAD re-fetch had no HEAD-405-to-GET
  fallback, unlike the pre-redirect loop -- a server/proxy that rejects
  HEAD outright (not just pre-redirect) surfaced `PermissionError` for a
  directory that actually exists. Now mirrors the pre-redirect loop's
  fallback.
- `HttpPath._listdir()` now retries once with a trailing slash if the
  slash-less path 404s (defensive: real redirecting servers already work
  via `requests`' default GET redirect-following, but a non-redirecting
  server/proxy previously had no fallback at all).
- `HttpWriteStream.close()` raised *before* marking the underlying stream
  closed on a failed upload, so a second `close()` call (context-manager
  `__exit__` cleanup, or GC via `IOBase.__del__`) silently retried the PUT.
  Now marks closed even on failure.
- `HttpPath.rmdir()`/`DavPath.rmdir()` never checked `is_dir()` before
  falling through to `unlink()` -- an empty directory's listing and a
  *file* whose body/PROPFIND response yields zero real entries are
  indistinguishable from `_listdir()` alone, so calling `rmdir()` on a
  file silently deleted it instead of raising `NotADirectoryError`
  (`os.rmdir()`'s ENOTDIR contract).

## [0.7.0] - 2026-07-11

### Added (new schemes, optional extras)
- `dav:`/`davs:` scheme (`pathlib_next.uri.schemes.webdav.DavPath`):
  extends `HttpPath` with WebDAV (RFC 4918) PROPFIND for real stat/listdir
  metadata (replacing HTML-index scraping) and PUT/DELETE/MKCOL/MOVE for
  full read/write access. Requests go to the equivalent `http:`/`https:`
  URL; `as_uri()` still reports `dav:`/`davs:`. Reuses the `http` extra,
  no new dependency. `rmdir()` is recursive by WebDAV spec, unlike
  `pathlib.Path.rmdir()`'s "must be empty" contract -- documented, not
  silent.
- `s3:` scheme (`pathlib_next.uri.schemes.s3.S3Path`,
  `s3://bucket/key/path`): read/write/list via `boto3`. New `s3` extra.
  S3 has no real directories -- `is_dir()` is prefix emulation (any
  object key under `"<path>/"`), `mkdir()` creates a zero-byte `"<path>/"`
  marker object, `rmdir()` requires no other keys under that prefix.
  `rename()` uses server-side `copy_object`+`delete_object` (same-bucket
  only) instead of the generic download+upload+delete `move()` fallback.

## [0.6.0] - 2026-07-11

### Added (new schemes, stdlib-only, no new deps)
- `data:` scheme (RFC 2397, `pathlib_next.uri.schemes.data.DataUri`):
  read-only, no backend/connection -- the entire file content is embedded
  in the URI (`data:[<mediatype>][;base64],<data>`). `stat().st_size` is
  the decoded payload length; `iterdir()` raises `NotADirectoryError`
  (it's always a single file); write operations raise `NotImplementedError`.
- `ftp:`/`ftps:` scheme (`pathlib_next.uri.schemes.ftp.FtpPath`): full
  read/write/list access via stdlib `ftplib`, with a thread-keyed LRU
  connection cache mirroring `sftp.py`. Listing/stat prefer MLSD (RFC
  3659); servers without it fall back to NLST (listing) and SIZE
  (file-only stat). Writes buffer in memory and upload via STOR/APPE on
  `close()`. `chmod()` uses the common but non-standard `SITE CHMOD`
  extension (may not be supported by every server).
- `zip:`/`tar:` archive paths (`pathlib_next.uri.schemes.archive`):
  `<scheme>:<archive-uri>!/<inner-path>` (Java-style `!/` separator,
  URI form proposed to and confirmed by the user before implementation).
  The archive half is itself any absolute URI with an explicit scheme, so
  archives are readable straight off any other backend (`file:`, `http:`,
  `sftp:`, `ftp:`, `data:`, ...). Read is supported for both schemes.
  Write is `zip:`-only, and only for brand-new entries in a local (`file:`)
  outer archive (overwriting/deleting/renaming an existing entry would
  need a full-archive rewrite -- not implemented, raises
  `NotImplementedError`). `tar:` auto-detects `.tar.gz`/`.tar.bz2`/`.tar.xz`
  compression and is always read-only.

## [0.5.0] - 2026-07-11

### Fixed (critical -- found while writing the examples)
- `Path("...")` -- the top-level dispatcher documented in this project's
  own README quick start and used throughout -- silently dropped its
  constructor arguments on Python <3.12, leaving a blank instance that
  crashed with `AttributeError: _drv` the moment anything touched it (e.g.
  the `/` operator). Masked on 3.12+, where the real parsing happens in
  `__init__` (called separately, with the original args, regardless of what
  `__new__` did) rather than `__new__` itself. Every one of the new suite's 300
  tests constructed via `LocalPath(...)` directly instead, so this went
  undetected until `examples/local_and_mem.py` exercised the documented
  `Path(...)` entry point end to end.

### Fixed (found by the new test suite, not in the original bug list)
- `LocalPath.stat()`/`chmod()` inherit directly from `pathlib.Path` via MRO
  and crashed with `TypeError` on Python 3.9 the moment anything passed
  `follow_symlinks=` (e.g. `Path.walk()`'s default `follow_symlinks=False`)
  -- now shimmed with `lstat()`/`lchmod()` on <3.10, same as the existing
  `FileUri` shim (which now just delegates to `LocalPath`).
- `MemPath.__init__` decided whether to propagate a parent's backend with
  `if _backend and backend is None:` -- an empty (but valid) backend dict is
  falsy, so joining off a freshly-created, empty `MemPath` silently gave the
  child a disconnected new backend instead of sharing the parent's.
- `MemPath.stat()` never set `st_size` for files (always defaulted to `0`),
  breaking any size-based checksum comparison (notably `PathSyncer`'s
  typical usage).
- `glob()`'s core algorithm decided whether to recurse into the *parent*
  directory using whether the *leaf* segment is a wildcard, instead of
  whether the *parent path itself* contains one. Since a wildcarded leaf
  with a literal parent directory is the overwhelmingly common case
  (`glob("*.py")`), this always took the "recurse into parent" branch,
  which only degenerated back to the correct single directory when the
  parent has a non-empty literal name to re-match against -- true for
  essentially every real filesystem path except an OS root. It silently
  returned the wrong result on `MemPath`'s virtual root (empty name).
- `HttpPath.iterdir()` gave every subdirectory entry an empty `.name`:
  directory-listing entries for subdirectories carry a trailing `/`
  (`htmllistparse`'s convention), which wasn't stripped before building the
  child's path, and `Pathname.name` derives from the last path segment --
  empty for a trailing-slash path.
- `SftpPath.rename()` resolved a plain string target relative to `self`
  (joining it as a child, e.g. `"/a.txt".rename("b.txt")` produced
  `"/a.txt/b.txt"`) instead of `self`'s parent (sibling rename).

### Added (test suite)
- Full pytest suite (`tests/`): pure-path parity against `pathlib.PurePosixPath`
  (`test_parity_pure.py`), local I/O parity against `pathlib.Path`/`os.walk`
  (`test_parity_io.py`), a reusable filesystem-contract mixin run against
  `LocalPath`/`MemPath`/`FileUri` and exported as `pathlib_next.testing.
  PathContract` for third-party `Path`/`UriPath` implementers
  (`test_contract.py`), glob vs. stdlib ground truth (`test_glob.py`), URI
  parsing/scheme-dispatch/query/source coverage, MemPath- and SFTP-specific
  unit tests (SFTP mocked, no real server), HTTP tests against a real stdlib
  `ThreadingHTTPServer`, and `PathSyncer` coverage. 300 tests, ~85% line
  coverage, green on both Python 3.9 and 3.13.

### Added (docs)
- `docs/guides/schemes.md` (capability matrix per scheme) and
  `docs/guides/extending.md` (both extension tracks, with worked examples
  and `pathlib_next.testing.PathContract` usage). Rewrote `docs/index.md`
  and the README with a 30-second example per scheme and a capability
  matrix. Class-level docstrings added across the package for the rendered
  API reference.

### Changed
- `examples/example.py` (an unstructured scratch script) split into three
  focused, runnable examples: `examples/local_and_mem.py` (self-contained,
  no network), `examples/http_listing.py` and `examples/sftp_sync.py`
  (network-touching, guarded under `if __name__ == "__main__"`,
  configurable via env vars, fail soft when unreachable/unconfigured).

### Added
- `Pathname.joinpath()`, `Pathname.full_match()` (3.13 parity, supports `**`
  matching any number of segments), `Pathname.anchor`/`drive`/`root`
  (generic derivation for non-local paths), `Path.rglob()`,
  `read_text(..., newline=)` (3.13 parity), `Path.samefile()` (default
  `st_dev`/`st_ino` comparison when the backend's `stat()` provides them,
  `NotImplementedError` otherwise).
- `Path.glob()`/`LocalPath.glob()`: `recursive=` now auto-detects (`True` if
  the pattern has a `"**"` component) instead of defaulting to `False`;
  explicit `recursive=True`/`False` still overrides.
- `Path.copy()`: raises `IsADirectoryError` when the target is an existing
  directory (previously misbehaved); gained `follow_symlinks=`/
  `preserve_metadata=` kwargs, named to match CPython 3.14's `Path.copy()`.
- `docs/divergences.md`: registry of every deliberate behavioral divergence
  from `pathlib`, with rationale. Linked from the docs nav.

### Fixed
- `Path.mkdir(parents=True)` created intermediate parents with `exist_ok=False`
  (racy, and wrong when a parent already existed) and dropped the caller's
  `exist_ok` on the final retry.
- `Path.touch(exist_ok=False)` silently truncated an existing file instead of
  raising `FileExistsError` (pathlib parity).
- `LocalPath.glob()`'s `dironly` parameter defaulted to `False`, which made the
  `is None` check for trailing-slash directory-only detection dead code.
- `Stat._st_mode()` only caught `FileNotFoundError`, letting `PermissionError`
  and other `OSError`s propagate out of `exists()`/`is_dir()`/etc. where pathlib
  returns `False`. Also fixed: `follow_symlinks` was accepted but never
  forwarded to the underlying `stat()` call, so `is_symlink()` never actually
  inspected the symlink itself.
- `MemPath._open()` treated any mode other than `"w"` as a read, so `"a"`/`"x"`
  silently misbehaved; now dispatches `r`/`w`/`x`/`a` correctly and raises
  `NotImplementedError` for anything else. `MemBytesIO.close()` used
  `seek(0);read()` instead of `getvalue()`, losing content if the caller's
  cursor wasn't already at position 0 when closing.
- `MemPath.normalized` mangled `".."`-escaping paths (e.g. `".."`) into `"."`;
  now normalizes against a virtual root so they clamp at the root instead.
- `PathAndStat.__getattr__()` returned `None` for any unrecognized attribute
  instead of raising `AttributeError`, breaking `hasattr()`-based logic.
- `parsedate(None)` / an unparseable date string returned "now" instead of
  epoch 0, which could poison `PathSyncer`'s checksum/freshness comparisons for
  HTTP sources with no `Last-Modified` header.
- `HttpPath.stat()` used a bare `except:`; cached `_isdir` from a response that
  hadn't been confirmed successful yet (including 404s); and didn't fall back
  to GET when a server rejected `HEAD` with 405.
- `uri.Query` no longer depends on `uritools`' private `_querydict`/`_querylist`
  helpers (reimplemented locally against the public `uriencode()`).
- `Uri` join (`_load_parts`): `query`/`fragment` are now resolved with the same
  "last segment that actually sets one wins" rule already used for `source`
  (previously any segment, even one with no query/fragment, would blank out an
  earlier segment's). Join semantics are now documented explicitly:
  pathlib-`joinpath`-like, not RFC 3986 reference resolution, `..` is never
  resolved during join.
- `Source.is_local()` (DNS lookup) and `get_machine_ips()` are now
  `functools.lru_cache`d -- previously ran on every call.

### Fixed (crash-level bugs)
- `MemPath.stat()`/`MemPath._open()` returned a `FileNotFoundError` instance instead
  of raising it for a missing path, causing an unrelated `AttributeError` downstream.
- `LRU.invalidate()` called `self.lock()` instead of using `self.lock` as a context
  manager (`RLock` isn't callable) -- broke the SFTP client reconnect path.
- `Pathname.match()` had reversed `isinstance()` arguments and compared against
  `str(self)` (which includes scheme/host for `Uri`) instead of `as_posix()`.
- Glob wildcard detection (`WILCARD_PATTERN`, renamed `WILDCARD_PATTERN`, old name
  kept as an alias) used `.match()` (anchored) instead of `.search()`, so patterns
  like `"foo*"` weren't recognized as wildcards.
- `Uri` was unhashable (defined `__eq__` without `__hash__`); `__eq__` now also
  returns `NotImplemented` for non-`Pathname`/`str` operands instead of raising.
- `Uri.is_relative_to()` used `str.startswith()` on normalized path strings, so
  `/foo/bar2` was incorrectly reported as relative to `/foo/bar`; now compares
  path segments.
- `Uri.relative_to(walk_up=True)` was dead code -- an early guard raised
  `ValueError` before the walk-up loop ever ran.
- `HttpPath.is_dir()`/`is_file()` tested truthiness of bound methods
  (`self._is_dir`, `self.is_dir`) instead of calling/checking the right attribute,
  so both always returned truthy nonsense.
- `SftpPath.chmod()` didn't accept `follow_symlinks=`, so the inherited `lchmod()`
  crashed with `TypeError`; now raises `NotImplementedError` for
  `follow_symlinks=False` (paramiko has no `lchmod`).
- `SftpPath` defined `_rename()`, which nothing ever called -- renamed to
  `rename()` so `move()`/`rename()` actually use SFTP's native rename instead of
  silently falling back to copy+unlink for every move.
- `Uri.__init__()` used a bare `except:` around `Path.as_uri()` (now
  `except ValueError:`, matching what `as_uri()` actually raises for relative
  paths) and crashed with `AttributeError` when constructing from an
  `os.PathLike` that only implements `__fspath__` (no `as_posix()`).
- `Path.rm(ignore_error=callable)` never actually called the callable -- both
  branches of its error handler returned the callable object itself.

### Fixed (Python 3.9/3.10 compatibility)
- Actual Python 3.9/3.10 runtime compatibility (CI previously only tested 3.11/3.13
  and missed these): `LocalPath`/`Uri` case-sensitivity and path-separator detection
  crashed on 3.9-3.11 (`_flavour` object has no `normcase`); `open(mode="r")` crashed
  on <3.10 (`io.text_encoding` is 3.10+); glob pattern compilation crashed on <3.11
  (`re.NOFLAG` is 3.11+); `FileUri.stat()`/`chmod()` crashed on 3.9
  (`pathlib.Path.stat/chmod` gained `follow_symlinks=` in 3.10; raises
  `NotImplementedError` there for `follow_symlinks=False`).
- `LocalPath._path_separators` returned the env-var list separator (`;`/`:`) instead
  of the path separator, and could include a `None` altsep on POSIX.

### Added
- `tests/test_smoke.py`: regression coverage for README/example snippets across
  supported Python versions.

## [0.4.1] - 2026-07-11

### Fixed
- Removed explicit `[tool.hatch.build.targets.wheel]` packages config that caused hatchling to fail resolving `README.md` during editable installs on CI.
- Converted `README.md` from a symlink (mode `120000`) to a regular file, fixing `git checkout` failures on macOS and Windows runners.
- Removed internal tooling references from committed files.

## [0.4.0] - 2026-07-11

### Added
- Standardized repository layout and relocated examples to `examples/` directory.
- Configured MkDocs documentation site with dynamic API reference using `mkdocstrings`.
- Added GitHub Actions workflows for matrix testing (`test.yml`) and release pipelines (`release.yml`).
- Added typing marker `py.typed` for PEP 561 compliance.

### Changed
- Added backward compatibility support for Python 3.9 and 3.10: added `from __future__ import annotations` across the codebase, refactored runtime-evaluated union types to use `typing.Union`, and provided fallbacks for `TypeAlias` and `ParamSpec`.
- Updated package requirement to `requires-python = ">=3.9"`.

## [0.3.5] - 2026-07-11

### Added
- Split path into protocols that can be standalone.
- Sync error handling.
- Generic Path Protocol based pathlib implementation for URI paths with file access support for sftp, http, file schemes.

[Unreleased]: https://github.com/jose-pr/pathlib_next/compare/v0.8.1...HEAD
[0.8.1]: https://github.com/jose-pr/pathlib_next/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/jose-pr/pathlib_next/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/jose-pr/pathlib_next/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/jose-pr/pathlib_next/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/jose-pr/pathlib_next/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/jose-pr/pathlib_next/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/jose-pr/pathlib_next/releases/tag/v0.4.0
[0.3.5]: https://github.com/jose-pr/pathlib_next/releases/tag/v0.3.5
