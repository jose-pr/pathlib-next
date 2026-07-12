import functools
import http.server
import threading
import time

import pytest

from pathlib_next.mempath import MemPath, MemPathBackend


@pytest.fixture
def fixture_tree(tmp_path):
    """A small fixture tree for glob/walk/contract tests:

    root/
      a.txt
      b.py
      .hidden.txt
      sub/
        c.py
        nested/
          d.py
      empty_dir/
    """
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.py").write_text("b")
    (tmp_path / ".hidden.txt").write_text("hidden")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("c")
    nested = sub / "nested"
    nested.mkdir()
    (nested / "d.py").write_text("d")
    (tmp_path / "empty_dir").mkdir()
    return tmp_path


@pytest.fixture
def mem_backend():
    return MemPathBackend()


@pytest.fixture
def mem_root(mem_backend):
    return MemPath("/", backend=mem_backend)


@pytest.fixture
def http_server(fixture_tree):
    """Serve fixture_tree over HTTP via stdlib's ThreadingHTTPServer with
    directory-listing support, on an OS-assigned port. Yields the base URL
    (no trailing slash)."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(fixture_tree)
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _CannedListingHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with `list_directory()` overridden to emit
    Apache-`<pre>`- or nginx-`<table>`-style listing HTML instead of the
    stdlib's bare `<ul><li>` -- the real `http_server` fixture never
    exercises `_DirectoryListingParser`'s two primary (non-fallback) code
    paths at all, since SimpleHTTPRequestHandler only ever emits `<ul>`.
    File GET/HEAD/redirect behavior is inherited unchanged from the stdlib
    handler; only the directory-index HTML differs. `listing_format` is a
    class attr set per-fixture via subclassing (not `functools.partial`,
    since we need to override a method, not just bind __init__ args)."""

    listing_format = "pre"  # or "table"

    def list_directory(self, path):
        import io
        import os
        import urllib.parse

        try:
            entries = os.listdir(path)
        except OSError:
            self.send_error(404, "No permission to list directory")
            return None
        entries.sort(key=lambda a: a.lower())

        rows = []
        for name in entries:
            fullname = os.path.join(path, name)
            is_dir = os.path.isdir(fullname)
            displayname = name + ("/" if is_dir else "")
            href = urllib.parse.quote(displayname)
            st = os.stat(fullname)
            mtime = time.strftime("%d-%b-%Y %H:%M:%S", time.localtime(st.st_mtime))
            size = "-" if is_dir else str(st.st_size)
            if self.listing_format == "pre":
                pad = " " * max(1, 45 - len(displayname))
                rows.append(f'<a href="{href}">{displayname}</a>{pad}{mtime}  {size}\n')
            else:
                rows.append(
                    f'<tr><td><a href="{href}">{displayname}</a></td>'
                    f"<td>{mtime}</td><td align=\"right\">{size}</td>"
                    f"<td>&nbsp;</td></tr>\n"
                )
        body = "".join(rows)

        title = f"Index of {self.path}"
        if self.listing_format == "pre":
            html = (
                f"<html><head><title>{title}</title></head><body>"
                f'<h1>{title}</h1><pre><a href="../">../</a>\n{body}</pre>'
                "</body></html>"
            )
        else:
            head = (
                '<tr><th><a href="?C=N;O=D">Name</a></th>'
                '<th><a href="?C=M;O=A">Last modified</a></th>'
                '<th><a href="?C=S;O=A">Size</a></th><th>Description</th></tr>\n'
                '<tr><th colspan="4"><hr></th></tr>\n'
            )
            parent = (
                '<tr><td><a href="../">Parent Directory</a></td>'
                '<td>&nbsp;</td><td align="right">-</td><td>&nbsp;</td></tr>\n'
            )
            html = (
                f"<html><head><title>{title}</title></head><body>"
                f"<h1>{title}</h1><table>\n{head}{parent}{body}"
                "</table></body></html>"
            )

        encoded = html.encode("utf-8", "surrogateescape")
        f = io.BytesIO(encoded)
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        return f

    def log_message(self, format, *args):
        pass


class _ApachePreListingHandler(_CannedListingHandler):
    listing_format = "pre"


class _NginxTableListingHandler(_CannedListingHandler):
    listing_format = "table"


def _start_canned_server(handler_cls, fixture_tree):
    handler = functools.partial(handler_cls, directory=str(fixture_tree))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def http_server_apache_pre(fixture_tree):
    """Like `http_server`, but directory listings are Apache
    `mod_autoindex`-style `<pre>` HTML -- drives
    `_DirectoryListingParser`'s `<pre>` code path end-to-end."""
    yield from _start_canned_server(_ApachePreListingHandler, fixture_tree)


@pytest.fixture
def http_server_nginx_table(fixture_tree):
    """Like `http_server`, but directory listings are nginx
    `autoindex`-style `<table>` HTML -- drives
    `_DirectoryListingParser`'s `<table>` code path end-to-end."""
    yield from _start_canned_server(_NginxTableListingHandler, fixture_tree)


@pytest.fixture
def http_status_server():
    """Serves canned status codes / delays on demand, driving
    `_translate_http_errors` from real `requests` exceptions instead of
    hand-constructed ones: GET/HEAD/PUT/DELETE `/<code>` returns that
    status; `/timeout` sleeps past any client-side timeout. Yields the
    base URL (no trailing slash)."""

    class _StatusHandler(http.server.BaseHTTPRequestHandler):
        def _handle(self):
            path = self.path.lstrip("/")
            if path == "timeout":
                time.sleep(5)
                return
            try:
                code = int(path)
            except ValueError:
                code = 404
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_HEAD = do_PUT = do_DELETE = _handle

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _WritableHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler + real PUT/DELETE against files on disk --
    for a genuine write-then-read-back round trip. Existing
    `test_http_write_*`/`test_http_unlink` tests only monkeypatch
    `requests.Session.request` and assert the request was sent; they never
    verify data actually persists server-side."""

    def do_PUT(self):
        import pathlib

        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        p = pathlib.Path(self.translate_path(self.path))
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.send_response(204 if existed else 201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        import pathlib

        p = pathlib.Path(self.translate_path(self.path))
        if not p.exists():
            self.send_error(404)
            return
        p.unlink()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


class _HeadRejectingHandler(http.server.SimpleHTTPRequestHandler):
    """GET works normally; HEAD always 405s -- drives `stat()`'s
    HEAD-405-fallback-to-GET path against a real server."""

    def do_HEAD(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass


@pytest.fixture
def http_writable_server(fixture_tree):
    yield from _start_canned_server(_WritableHandler, fixture_tree)


@pytest.fixture
def http_server_head_rejecting(fixture_tree):
    yield from _start_canned_server(_HeadRejectingHandler, fixture_tree)


@pytest.fixture
def unused_tcp_port():
    """A port nobody is listening on, for connection-refused coverage."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def ftp_server(fixture_tree):
    import threading
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    authorizer = DummyAuthorizer()
    authorizer.add_user("user", "12345", str(fixture_tree), perm="elradfmwMT")
    handler = FTPHandler
    handler.authorizer = authorizer
    server = FTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.socket.getsockname()[1]
        yield f"ftp://user:12345@127.0.0.1:{port}/"
    finally:
        server.close_all()
        thread.join(timeout=5)


@pytest.fixture
def dav_server(fixture_tree):
    import time
    import threading
    import wsgidav.wsgidav_app
    from cheroot import wsgi

    config = {
        "host": "127.0.0.1",
        "port": 0,
        "provider_mapping": {"/": str(fixture_tree)},
        "simple_dc": {"user_mapping": {"*": True}},
        "logging": {"enable": False}
    }
    app = wsgidav.wsgidav_app.WsgiDAVApp(config)
    server = wsgi.Server(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    while not server.ready:
        time.sleep(0.01)
    try:
        yield f"dav://127.0.0.1:{server.bind_addr[1]}"
    finally:
        server.stop()
        thread.join(timeout=5)


@pytest.fixture
def s3_server(fixture_tree):
    """In-process moto S3 mock with a pre-populated 'test-bucket' matching
    the standard fixture_tree layout required by ReadPathContract/PathContract."""
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws
    import os
    import pathlib

    def _upload_tree(client, bucket, local_dir, prefix=""):
        local_dir = pathlib.Path(local_dir)
        for entry in local_dir.iterdir():
            rel = (prefix + "/" + entry.name).lstrip("/")
            if entry.is_dir():
                # upload a marker so the directory appears in listings
                client.put_object(Bucket=bucket, Key=rel + "/", Body=b"")
                _upload_tree(client, bucket, entry, rel)
            else:
                client.put_object(Bucket=bucket, Key=rel, Body=entry.read_bytes())

    with mock_aws():
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        _upload_tree(client, "test-bucket", fixture_tree)
        yield "s3://test-bucket", None


@pytest.fixture
def sftp_server(fixture_tree):
    """In-process SFTP server backed by fixture_tree, using asyncssh's own
    `SFTPServer` (chrooted) -- a full local-filesystem-backed SFTP server
    for free, replacing what used to be a ~150-line hand-rolled paramiko
    `ServerInterface`/`SFTPServerInterface`. A CLIENT backend choice
    (paramiko or asyncssh, see `TestSftpContract`) is independent of which
    library the test SERVER uses -- an asyncssh server talks standard SFTP
    to a paramiko client fine (verified). Runs on its own dedicated event
    loop/thread, independent of `AsyncsshSftpBackend`'s shared bridge loop
    (server and client are logically separate machines in reality; keeping
    them on separate loops here mirrors that instead of coupling test
    infrastructure to backend-internal state)."""
    asyncssh = pytest.importorskip("asyncssh")
    import asyncio
    import sys

    root = str(fixture_tree)

    class _NoAuth(asyncssh.SSHServer):
        def begin_auth(self, username):
            return False

    def _sftp_factory(chan):
        return asyncssh.SFTPServer(chan, chroot=root)

    async def _start():
        return await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=_NoAuth,
            server_host_keys=[asyncssh.generate_private_key("ssh-rsa")],
            sftp_factory=_sftp_factory,
            process_factory=None,
        )

    # SelectorEventLoop on Windows, not the WindowsProactorEventLoopPolicy
    # default -- avoids a benign-but-noisy Proactor pipe-transport __del__
    # warning (see _asyncssh.py's _new_loop() for the same fix applied to
    # the real backend's bridge loop); we don't need subprocess pipes here.
    loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = asyncio.run_coroutine_threadsafe(_start(), loop).result()
    port = server.sockets[0].getsockname()[1]
    try:
        # A password is included even though the server's begin_auth()
        # accepts everything unconditionally: paramiko's SSHClient.connect()
        # only sends an auth attempt at all if at least one of
        # key/keyfiles/password is available -- with allow_agent=False,
        # look_for_keys=False and no password, it raises "No authentication
        # methods available" client-side before ever reaching the server.
        yield f"sftp://x:x@127.0.0.1:{port}/"
    finally:
        # server.close() must run ON THE LOOP'S OWN THREAD -- asyncio/
        # asyncssh objects aren't thread-safe, and calling close() directly
        # from this (test) thread races the loop thread still processing
        # I/O, corrupting internal state (observed: TypeError from
        # asyncio.Server._wakeup() on `self._waiters` -- not a double
        # -close, a torn read of state being mutated concurrently).
        # Not blocking on wait_closed(): it waits for every accepted
        # connection to close too, and a long-lived cached client
        # (paramiko's connection cache, or an asyncssh backend instance a
        # test didn't explicitly tear down) can leave one open, hanging
        # this indefinitely.
        loop.call_soon_threadsafe(server.close)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


def _build_git_tree(local_dir):
    """Flatten a local directory into a git-tree-shaped {posix relpath:
    bytes} dict for the fake GitHub/GitLab servers below. Git has no
    concept of an empty directory (no tree entry can represent one with no
    blobs under it) -- a truly empty dir (fixture_tree's `empty_dir/`)
    would vanish from any listing derived purely from blob-path prefixes,
    so a `.gitkeep`-style placeholder blob is synthesized under it here,
    same as a real repo author would have to do. This only affects the
    fake server's in-memory tree, never `fixture_tree` itself."""
    import pathlib

    local_dir = pathlib.Path(local_dir)
    tree = {}
    for path in sorted(local_dir.rglob("*")):
        rel = path.relative_to(local_dir).as_posix()
        if path.is_dir():
            if not any(path.iterdir()):
                tree[f"{rel}/.gitkeep"] = b""
        else:
            tree[rel] = path.read_bytes()
    return tree


def _git_tree_children(tree, prefix):
    """Immediate children of `prefix` ("" for root) in a `_build_git_tree`
    dict -- name -> "dir"/"file", mirroring how GitHub's contents API and
    GitLab's tree API both report one directory level at a time."""
    prefix = f"{prefix}/" if prefix else ""
    children = {}
    for key in tree:
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        if not rest:
            continue
        name, sep, _ = rest.partition("/")
        children[name] = "dir" if sep else "file"
    return children


@pytest.fixture
def github_api_server(fixture_tree):
    """Faithful (not mocked) fake of the subset of the GitHub REST contents
    API (`GET /repos/{owner}/{repo}/contents/{path}`) that `GitHubPath`
    actually calls: directory listing (JSON array), file metadata
    (JSON object, base64 `content`), and raw file bodies (`Accept:
    .../vnd.github.raw+json`). Two refs are served (`main` the default,
    `other-branch` with a divergent `a.txt`) so ref plumbing is verified
    end-to-end, not just default-branch reads. Yields
    `(base_url, owner, repo)`."""
    import base64
    import json
    import urllib.parse

    main_tree = _build_git_tree(fixture_tree)
    other_tree = dict(main_tree)
    other_tree["a.txt"] = b"a-on-other-branch"
    refs = {"main": main_tree, "other-branch": other_tree}
    owner, repo = "acme", "widgets"

    class _GitHubApiHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(split.query)
            ref = qs.get("ref", ["main"])[0]
            tree = refs.get(ref)
            if tree is None:
                self._send_json(404, {"message": "Not Found"})
                return

            prefix = f"/repos/{owner}/{repo}/contents"
            if split.path == prefix:
                subpath = ""
            elif split.path.startswith(prefix + "/"):
                subpath = urllib.parse.unquote(split.path[len(prefix) + 1 :])
            else:
                self._send_json(404, {"message": "Not Found"})
                return

            if subpath in tree:
                self._send_file(subpath, tree[subpath])
                return

            children = _git_tree_children(tree, subpath)
            if children or subpath == "":
                entries = []
                for name, kind in sorted(children.items()):
                    childpath = f"{subpath}/{name}" if subpath else name
                    size = len(tree.get(childpath, b"")) if kind == "file" else 0
                    entries.append(
                        {"name": name, "path": childpath, "type": kind, "size": size}
                    )
                self._send_json(200, entries)
                return

            self._send_json(404, {"message": "Not Found"})

        def _send_file(self, subpath, content):
            accept = self.headers.get("Accept", "")
            if "raw" in accept:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            self._send_json(
                200,
                {
                    "name": subpath.rsplit("/", 1)[-1],
                    "path": subpath,
                    "type": "file",
                    "size": len(content),
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                },
            )

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GitHubApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", owner, repo
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def gitlab_api_server(fixture_tree):
    """Faithful (not mocked) fake of the subset of the GitLab REST API v4
    that `GitLabPath` actually calls: `.../repository/tree` (listing, no
    size -- matches the real endpoint's shape), `.../repository/files/:path`
    (file metadata) and `.../repository/files/:path/raw` (file body). Yields
    `(base_url, owner, repo)`."""
    import json
    import urllib.parse

    tree = _build_git_tree(fixture_tree)
    owner, repo = "acme", "widgets"
    project_id = urllib.parse.quote(f"{owner}/{repo}", safe="")

    class _GitLabApiHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(split.query)

            # `GET /projects/:id` (default_branch lookup) -- no "/repository/"
            # suffix, must be checked before the repository-scoped prefix below.
            if split.path == f"/api/v4/projects/{project_id}":
                self._send_json(200, {"default_branch": "main"})
                return

            prefix = f"/api/v4/projects/{project_id}/repository/"

            if not split.path.startswith(prefix):
                self._send_json(404, {"message": "404 Not Found"})
                return
            rest = split.path[len(prefix) :]

            if rest == "tree":
                path = qs.get("path", [""])[0]
                children = _git_tree_children(tree, path)
                entries = [
                    {
                        "id": f"fake-{name}",
                        "name": name,
                        "type": "tree" if kind == "dir" else "blob",
                        "path": f"{path}/{name}" if path else name,
                        "mode": "040000" if kind == "dir" else "100644",
                    }
                    for name, kind in sorted(children.items())
                ]
                self._send_json(200, entries)
                return

            if rest.startswith("files/"):
                filerest = rest[len("files/") :]
                raw = filerest.endswith("/raw")
                encoded_path = filerest[: -len("/raw")] if raw else filerest
                path = urllib.parse.unquote(encoded_path)
                # Real GitLab requires `ref` on the files endpoints (both
                # metadata and /raw) -- omitting it is a 400, not a
                # default-branch fallback like the tree endpoint gets
                # (confirmed live against gitlab.com; GitLabPath resolves
                # and sends it, this just mirrors the real requirement).
                if "ref" not in qs:
                    self._send_json(400, {"error": "ref is missing, ref is empty"})
                    return
                content = tree.get(path)
                if content is None:
                    self._send_json(404, {"message": "404 File Not Found"})
                    return
                if raw:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                self._send_json(
                    200,
                    {
                        "file_name": path.rsplit("/", 1)[-1],
                        "file_path": path,
                        "size": len(content),
                    },
                )
                return

            self._send_json(404, {"message": "404 Not Found"})

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GitLabApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", owner, repo
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def gcs_api_server(fixture_tree):
    """Faithful (not mocked) fake of the Google Cloud Storage JSON REST API
    subset that `GsPath` actually calls: list, get, put, delete. Yields
    `(base_url, bucket_name)`."""
    import json
    import urllib.parse
    from pathlib_next.uri.schemes.gs import GsBackend

    # Build an in-memory object store from fixture_tree
    objects = {}
    for item in fixture_tree.rglob("*"):
        if item.is_file():
            rel = item.relative_to(fixture_tree)
            key = str(rel).replace("\\", "/")
            objects[key] = item.read_bytes()

    bucket_name = "test-bucket"

    class _GcsApiHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(split.query)

            # LIST: /storage/v1/b/{bucket}/o?prefix=&delimiter=/
            if split.path == f"/storage/v1/b/{bucket_name}/o":
                prefix = qs.get("prefix", [""])[0]
                delimiter = qs.get("delimiter", [None])[0]
                result = {"kind": "storage#objects", "items": [], "prefixes": []}
                prefixes_set = set()
                for key in sorted(objects.keys()):
                    if key.startswith(prefix):
                        rest = key[len(prefix):]
                        if delimiter:
                            parts = rest.split(delimiter, 1)
                            if len(parts) > 1:
                                pre = prefix + parts[0] + delimiter
                                if pre not in prefixes_set:
                                    prefixes_set.add(pre)
                                    result["prefixes"].append(pre)
                            else:
                                result["items"].append({
                                    "kind": "storage#object",
                                    "name": key,
                                    "size": str(len(objects[key])),
                                    "updated": "2026-07-12T00:00:00Z",
                                })
                        else:
                            result["items"].append({
                                "kind": "storage#object",
                                "name": key,
                                "size": str(len(objects[key])),
                                "updated": "2026-07-12T00:00:00Z",
                            })
                self._send_json(200, result)
                return

            # GET metadata: /storage/v1/b/{bucket}/o/{object}
            if split.path.startswith(f"/storage/v1/b/{bucket_name}/o/"):
                obj_name = urllib.parse.unquote(split.path[len(f"/storage/v1/b/{bucket_name}/o/"):])
                if obj_name in objects:
                    self._send_json(200, {
                        "kind": "storage#object",
                        "name": obj_name,
                        "size": str(len(objects[obj_name])),
                        "updated": "2026-07-12T00:00:00Z",
                    })
                    return
                self._send_json(404, {})
                return

            # GET raw body: /storage/v1/b/{bucket}/o/{object}?alt=media
            if split.path.startswith(f"/storage/v1/b/{bucket_name}/o/") and qs.get("alt") == ["media"]:
                obj_name = urllib.parse.unquote(split.path[len(f"/storage/v1/b/{bucket_name}/o/"):])
                if obj_name in objects:
                    content = objects[obj_name]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                self._send_json(404, {})
                return

            # PUT (upload): /upload/storage/v1/b/{bucket}/o?uploadType=media&name=
            if split.path == f"/upload/storage/v1/b/{bucket_name}/o":
                obj_name = qs.get("name", [""])[0]
                if obj_name:
                    content_length = int(self.headers.get("Content-Length", 0))
                    objects[obj_name] = self.rfile.read(content_length)
                    self._send_json(200, {
                        "kind": "storage#object",
                        "name": obj_name,
                        "size": str(len(objects[obj_name])),
                        "updated": "2026-07-12T00:00:00Z",
                    })
                    return
                self._send_json(400, {})
                return

            # DELETE: /storage/v1/b/{bucket}/o/{object}
            if split.path.startswith(f"/storage/v1/b/{bucket_name}/o/"):
                obj_name = urllib.parse.unquote(split.path[len(f"/storage/v1/b/{bucket_name}/o/"):])
                if obj_name in objects:
                    del objects[obj_name]
                    self.send_response(204)
                    self.end_headers()
                    return
                self._send_json(404, {})
                return

            self._send_json(404, {})

        def do_PUT(self):
            # Delegate to GET handler for PUT uploads
            self.do_GET()

        def do_DELETE(self):
            # Delegate to GET handler for DELETE
            self.do_GET()

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GcsApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        yield base_url, bucket_name
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def gs_server(gcs_api_server):
    """GCS test server that returns configured BaseGsBackend and a GsPath."""
    from pathlib_next.uri.schemes.gs import GsBackend, GsPath
    base_url, bucket_name = gcs_api_server
    backend = GsBackend(
        client_options={"api_endpoint": base_url}
    )
    return GsPath(f"gs://{bucket_name}", backend=backend), backend


@pytest.fixture
def az_api_server(fixture_tree):
    """Faithful (not mocked) fake of the Azure Blob Storage XML REST API
    subset that `AzPath` actually calls. Yields `(base_url, account, container)`."""
    import urllib.parse
    from xml.etree import ElementTree as ET

    objects = {}
    for item in fixture_tree.rglob("*"):
        if item.is_file():
            rel = item.relative_to(fixture_tree)
            key = str(rel).replace("\\", "/")
            objects[key] = item.read_bytes()

    account = "testaccount"
    container = "testcontainer"

    class _AzApiHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(split.query)

            # LIST: /{account}/{container}?restype=container&comp=list
            if (split.path == f"/{account}/{container}" and
                qs.get("restype") == ["container"] and
                qs.get("comp") == ["list"]):
                prefix = qs.get("prefix", [""])[0]
                delimiter = qs.get("delimiter", [None])[0]

                root = ET.Element("EnumerationResults")
                root.set("ContainerName", container)

                blobs_elem = ET.SubElement(root, "Blobs")
                prefixes_set = set()

                for key in sorted(objects.keys()):
                    if key.startswith(prefix):
                        rest = key[len(prefix):]
                        if delimiter:
                            parts = rest.split(delimiter, 1)
                            if len(parts) > 1:
                                pre = prefix + parts[0] + delimiter
                                if pre not in prefixes_set:
                                    prefixes_set.add(pre)
                                    pre_elem = ET.SubElement(root, "BlobPrefix")
                                    name_elem = ET.SubElement(pre_elem, "Name")
                                    name_elem.text = pre
                            else:
                                blob_elem = ET.SubElement(blobs_elem, "Blob")
                                name_elem = ET.SubElement(blob_elem, "Name")
                                name_elem.text = key
                                props_elem = ET.SubElement(blob_elem, "Properties")
                                size_elem = ET.SubElement(props_elem, "Content-Length")
                                size_elem.text = str(len(objects[key]))
                                mtime_elem = ET.SubElement(props_elem, "Last-Modified")
                                mtime_elem.text = "2026-07-12T00:00:00Z"
                        else:
                            blob_elem = ET.SubElement(blobs_elem, "Blob")
                            name_elem = ET.SubElement(blob_elem, "Name")
                            name_elem.text = key
                            props_elem = ET.SubElement(blob_elem, "Properties")
                            size_elem = ET.SubElement(props_elem, "Content-Length")
                            size_elem.text = str(len(objects[key]))
                            mtime_elem = ET.SubElement(props_elem, "Last-Modified")
                            mtime_elem.text = "2026-07-12T00:00:00Z"

                body = ET.tostring(root, encoding="utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            # GET blob or HEAD: /{account}/{container}/{blob}
            if split.path.startswith(f"/{account}/{container}/"):
                blob_name = urllib.parse.unquote(split.path[len(f"/{account}/{container}/"):])
                if blob_name in objects:
                    content = objects[blob_name]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("x-ms-blob-type", "BlockBlob")
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(content)
                    return
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(404)
            self.end_headers()

        def do_HEAD(self):
            # Delegate to GET
            self.do_GET()

        def do_PUT(self):
            split = urllib.parse.urlsplit(self.path)

            if split.path.startswith(f"/{account}/{container}/"):
                blob_name = urllib.parse.unquote(split.path[len(f"/{account}/{container}/"):])
                content_length = int(self.headers.get("Content-Length", 0))
                objects[blob_name] = self.rfile.read(content_length)
                self.send_response(201)
                self.end_headers()
                return

            self.send_response(400)
            self.end_headers()

        def do_DELETE(self):
            split = urllib.parse.urlsplit(self.path)

            if split.path.startswith(f"/{account}/{container}/"):
                blob_name = urllib.parse.unquote(split.path[len(f"/{account}/{container}/"):])
                if blob_name in objects:
                    del objects[blob_name]
                    self.send_response(202)
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AzApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        yield base_url, account, container
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def az_server(az_api_server):
    """Azure test server that returns configured BaseAzBackend and an AzPath."""
    from pathlib_next.uri.schemes.az import AzBackend, AzPath
    base_url, account, container = az_api_server
    conn_str = (
        f"DefaultEndpointsProtocol=http;AccountName={account};"
        f"AccountKey=dGVzdGtleQ==;BlobEndpoint={base_url}/{account};"
    )
    backend = AzBackend(connection_string=conn_str)
    return AzPath(f"az://{account}/{container}", backend=backend), backend
