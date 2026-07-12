import asyncio
import contextlib
import os
import statistics
import timeit
import tempfile
import shutil
import functools
import http.server
import threading
import time
from pathlib import Path as StdlibPath

# Add src to sys.path so we can import pathlib_next without installing it
import sys
sys.path.insert(0, str(StdlibPath(__file__).parent.parent / "src"))

from pathlib_next import Uri, UriPath, LocalPath
from pathlib_next.mempath import MemPath, MemPathBackend

def benchmark_uri_parse():
    # Parse a typical complex URI 10,000 times
    setup = "from pathlib_next import Uri"
    code = "Uri('http://user:pass@host:80/path/to/resource?query=1#fragment')"
    return timeit.timeit(code, setup=setup, number=10000)

def benchmark_uri_parse_unique():
    # Uri() construction is lazy (no parsing until .source/.path/... is
    # first accessed) and benchmark_uri_parse() above reuses the SAME
    # literal string every call, so it measures neither real parse cost.
    # This one forces the actual parse (the one-pass
    # _parse_uri) with a UNIQUE URI per iteration -- repeated-URI
    # microbenchmarks are not representative of real workloads and can
    # flatter/mislead by an order of magnitude if the underlying string
    # library caches by input (verified true of urllib.urlsplit, ~28x;
    # not true of uritools/our own parser, but always bench unique inputs
    # regardless so this stays an apples-to-apples comparison).
    from pathlib_next import Uri
    n = 5000
    urls = [
        f"http://user:pass@host{i}.example.com:8080"
        f"/path/to/resource{i}?query={i}#fragment{i}"
        for i in range(n)
    ]

    def _run():
        for url in urls:
            u = Uri(url)
            _ = u.source
            _ = u.path
            _ = u.query
            _ = u.fragment

    total = timeit.timeit(_run, number=1)
    return total / n * 1e6  # microseconds per unique Uri() parse

def benchmark_uri_parse_and_compose_unique():
    # Full round trip: parse (forced, unique URIs) + compose (as_uri(),
    # uncached -- sanitize=True forces a fresh compose every call instead
    # of hitting the cached-after-first-call fast path) -- exercises both
    # the one-pass parse (_parse_uri) and direct-assembly compose
    # (_format_parsed_parts) together, which is what "Uri() construction"
    # means end to end.
    from pathlib_next import Uri
    n = 5000
    urls = [
        f"http://user:pass@host{i}.example.com:8080"
        f"/path/to/resource{i}?query={i}#fragment{i}"
        for i in range(n)
    ]

    def _run():
        for url in urls:
            u = Uri(url)
            _ = u.as_uri(sanitize=True)

    total = timeit.timeit(_run, number=1)
    return total / n * 1e6  # microseconds per unique Uri() parse+compose

def benchmark_path_join():
    # Join paths using / operator 10,000 times
    setup = "from pathlib_next import Uri; p = Uri('http://host/path')"
    code = "p / 'sub' / 'child'"
    return timeit.timeit(code, setup=setup, number=10000)

def benchmark_segments_name_access():
    # Access .segments and .name 10,000 times
    setup = "from pathlib_next import Uri; u = Uri('http://host/a/b/c/d/e')"
    code = "for _ in range(10000): _ = u.segments; _ = u.name"
    return timeit.timeit(code, setup=setup, number=1)

def benchmark_suffix_stem():
    # Access .suffix and .stem 10,000 times
    setup = "from pathlib_next import Uri; u = Uri('http://host/path/file.tar.gz')"
    code = "for _ in range(10000): _ = u.suffix; _ = u.stem"
    return timeit.timeit(code, setup=setup, number=1)

def benchmark_glob_mempath():
    # Glob over a 1k-file MemPath tree
    backend = MemPathBackend()
    root = MemPath("/", backend=backend)
    # Build 10 x 10 x 10 tree = 1000 files
    for i in range(10):
        for j in range(10):
            for k in range(10):
                p = root / f"dir_{i}" / f"sub_{j}" / f"file_{k}.txt"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("content")
    
    # Run glob 20 times
    t = timeit.timeit(lambda: list(root.glob("**/*.txt")), number=20)
    return t

def benchmark_localpath_vs_stdlib():
    # Measure LocalPath construct + stat vs raw pathlib.Path
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_file = StdlibPath(tmpdir) / "test.txt"
        temp_file.write_text("hello")
        
        setup_local = f"from pathlib_next import LocalPath; p = LocalPath({repr(str(temp_file))})"
        code_local = "p.stat()"
        t_local = timeit.timeit(code_local, setup=setup_local, number=2000)
        
        setup_std = f"from pathlib import Path; p = Path({repr(str(temp_file))})"
        code_std = "p.stat()"
        t_std = timeit.timeit(code_std, setup=setup_std, number=2000)
        
        ratio = t_local / t_std if t_std > 0 else 0.0
        return t_local, t_std, ratio


def benchmark_localpath_matrix():
    """Compare LocalPath against pathlib.Path on common local hot paths."""
    rows = []
    with tempfile.TemporaryDirectory() as tmpdir:
        root = StdlibPath(tmpdir)
        payload = os.urandom(64 * 1024)

        for i in range(6):
            for j in range(8):
                subdir = root / f"dir_{i}" / f"sub_{j}"
                subdir.mkdir(parents=True, exist_ok=True)
                for k in range(5):
                    (subdir / f"file_{k}.txt").write_text(
                        f"{i}-{j}-{k}\n", encoding="utf-8"
                    )

        sample_file = root / "dir_0" / "sub_0" / "sample.bin"
        sample_file.write_bytes(payload)

        def add_case(name, local_operation, std_operation, *, repeat=5):
            t_local = _measure(local_operation, repeat=repeat)
            t_std = _measure(std_operation, repeat=repeat)
            ratio = t_local / t_std if t_std > 0 else float("inf")
            rows.append((name, t_local, t_std, ratio))

        add_case(
            "construct path (10k)",
            lambda: [
                LocalPath(str(sample_file))
                for _ in range(10000)
            ],
            lambda: [
                StdlibPath(str(sample_file))
                for _ in range(10000)
            ],
            repeat=3,
        )
        add_case(
            "join path (10k)",
            lambda base=LocalPath(str(root)): [
                base / "alpha" / "beta" / "gamma.txt"
                for _ in range(10000)
            ],
            lambda base=StdlibPath(str(root)): [
                base / "alpha" / "beta" / "gamma.txt"
                for _ in range(10000)
            ],
            repeat=3,
        )
        add_case(
            "stat() file (2k)",
            lambda path=LocalPath(str(sample_file)): [
                path.stat()
                for _ in range(2000)
            ],
            lambda path=StdlibPath(str(sample_file)): [
                path.stat()
                for _ in range(2000)
            ],
            repeat=3,
        )
        add_case(
            "read_bytes() 64 KiB",
            lambda path=LocalPath(str(sample_file)): path.read_bytes(),
            lambda path=StdlibPath(str(sample_file)): path.read_bytes(),
            repeat=5,
        )
        add_case(
            "iterdir() 8 entries",
            lambda path=LocalPath(str(root / "dir_0")): list(path.iterdir()),
            lambda path=StdlibPath(str(root / "dir_0")): list(path.iterdir()),
            repeat=5,
        )
        add_case(
            "glob('**/*.txt') 240 files",
            lambda path=LocalPath(str(root)): list(path.glob("**/*.txt")),
            lambda path=StdlibPath(str(root)): list(path.glob("**/*.txt")),
            repeat=3,
        )
    return rows

def benchmark_walk_glob_http():
    # Set up a small temp directory for HTTP server
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = StdlibPath(tmpdir)
        # Create a small tree (20 files)
        for i in range(4):
            sub = tmp_path / f"dir_{i}"
            sub.mkdir()
            for j in range(5):
                (sub / f"file_{j}.html").write_text("data")
        
        class QuietSimpleHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format, *args):
                return

        handler = functools.partial(
            QuietSimpleHTTPRequestHandler, directory=str(tmp_path)
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        
        try:
            # We import the HttpPath scheme
            from pathlib_next.uri.schemes.http import HttpPath
            url = f"http://127.0.0.1:{port}"
            
            # Warm up
            hp = HttpPath(url)
            _ = list(hp.glob("**/*.html"))
            
            # Benchmark glob 10 times
            t_glob = timeit.timeit(lambda: list(hp.glob("**/*.html")), number=10)
            
            # Benchmark walk 10 times
            t_walk = timeit.timeit(lambda: list(hp.walk()), number=10)
            
            return t_glob, t_walk
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

def benchmark_http_directory_parser():
    # benchmark_walk_glob_http() above serves fixture files through stdlib
    # SimpleHTTPRequestHandler, whose listing is a bare <ul><li> -- that
    # only ever exercises _DirectoryListingParser's `all_links` fallback
    # branch, never its two primary (and far more common in the real
    # world) Apache-<pre>/nginx-<table> code paths. This benchmarks those
    # directly against realistic synthetic HTML (during the replacement
    # work this in-house parser was verified equivalent to, and 2.9x-6.2x faster
    # than, the bs4+html5lib+htmllistparse implementation it replaced).
    from pathlib_next.uri.schemes.http import _DirectoryListingParser

    def make_apache_pre(n):
        rows = ['<a href="../">../</a>\n']
        for i in range(n):
            is_dir = i % 7 == 0
            name = f"dir_{i}/" if is_dir else f"file_{i:04d}.txt"
            size = "-" if is_dir else f"{(i % 900) + 1}.{i % 10}K"
            rows.append(
                f'<a href="{name}">{name}</a>'
                + " " * max(1, 50 - len(name))
                + f"11-Jul-2026 10:{i % 60:02d}:00    {size}\n"
            )
        body = "".join(rows)
        return (
            "<html><head><title>Index of /files/</title></head><body>"
            "<h1>Index of /files/</h1><pre>" + body + "</pre></body></html>"
        )

    def make_nginx_table(n):
        head = (
            '<tr><th><a href="?C=N;O=D">Name</a></th>'
            '<th><a href="?C=M;O=A">Last modified</a></th>'
            '<th><a href="?C=S;O=A">Size</a></th><th>Description</th></tr>\n'
            '<tr><th colspan="4"><hr></th></tr>\n'
            '<tr><td><a href="/files/">Parent Directory</a></td>'
            '<td>&nbsp;</td><td align="right">-</td><td>&nbsp;</td></tr>\n'
        )
        rows = [head]
        for i in range(n):
            is_dir = i % 7 == 0
            name = f"dir_{i}/" if is_dir else f"file_{i:04d}.txt"
            size = "-" if is_dir else f"{(i % 900) + 1}.{i % 10}K"
            rows.append(
                f'<tr><td><a href="{name}">{name}</a></td>'
                f'<td>2026-07-11 10:{i % 60:02d}  </td>'
                f'<td align="right">{size}</td><td>&nbsp;</td></tr>\n'
            )
        body = "".join(rows)
        return (
            "<html><head><title>Index of /files/</title></head><body>"
            "<h1>Index of /files/</h1><table>\n" + body + "</table></body></html>"
        )

    def parse(html):
        parser = _DirectoryListingParser()
        parser.feed(html)
        parser.close()
        return parser.listing

    n = 1000
    pre_html = make_apache_pre(n)
    table_html = make_nginx_table(n)
    t_pre = timeit.timeit(lambda: parse(pre_html), number=20)
    t_table = timeit.timeit(lambda: parse(table_html), number=20)
    return t_pre / 20 * 1000, t_table / 20 * 1000  # ms/parse, n=1000 entries


@contextlib.contextmanager
def _loopback_sftp_server():
    try:
        import asyncssh
    except ImportError as error:
        raise RuntimeError("asyncssh is required for local SFTP benchmarks") from error

    root_tmp = tempfile.TemporaryDirectory()
    root = StdlibPath(root_tmp.name)

    class _NoAuth(asyncssh.SSHServer):
        def begin_auth(self, username):
            return False

    def _sftp_factory(chan):
        return asyncssh.SFTPServer(chan, chroot=str(root))

    async def _start():
        return await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=_NoAuth,
            server_host_keys=[asyncssh.generate_private_key("ssh-rsa")],
            sftp_factory=_sftp_factory,
            process_factory=None,
        )

    async def _stop(server):
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0.1)

    loop = (
        asyncio.SelectorEventLoop()
        if sys.platform == "win32"
        else asyncio.new_event_loop()
    )
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = asyncio.run_coroutine_threadsafe(_start(), loop).result()
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"sftp://x:x@127.0.0.1:{port}/", root
    finally:
        try:
            asyncio.run_coroutine_threadsafe(_stop(server), loop).result(timeout=5)
        except Exception:
            loop.call_soon_threadsafe(server.close)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        root_tmp.cleanup()


def _write_tree(root: StdlibPath, files: dict[str, bytes | str]) -> None:
    for relpath, data in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")


def _measure(operation, *, repeat=5, warmup=True):
    if warmup:
        operation()
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        operation()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def _measure_status(operation, *, repeat=1, warmup=False):
    samples = []
    try:
        if warmup:
            operation()
        for _ in range(repeat):
            start = time.perf_counter()
            operation()
            samples.append(time.perf_counter() - start)
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    return statistics.median(samples)


def _fmt_metric(value):
    if isinstance(value, (int, float)):
        return f"{value:.4f}s"
    return str(value)


def _fmt_ratio(value):
    if isinstance(value, (int, float)):
        return f"{value:.2f}x"
    return str(value)


def benchmark_sftp_backends():
    """Benchmark paramiko vs asyncssh across a broad SFTP operation matrix.

    Uses an in-process loopback asyncssh server so the two client backends
    hit the same target filesystem with identical data and no external
    host dependency. The numbers therefore emphasize backend overhead and
    request/throughput behavior more than WAN latency. The expensive
    recursive-copy stress case is opt-in via
    PATHLIB_NEXT_BENCH_SFTP_RECURSIVE=1 so the default suite stays fast
    and avoids leaving a timed-out async bridge task behind.
    """
    try:
        import paramiko
        from pathlib_next.uri.schemes.sftp import (
            AsyncsshSftpBackend,
            SftpBackend,
            SftpPath,
            _asyncssh as asyncssh_backend_mod,
        )
        from pathlib_next.uri.schemes.sftp._paramiko import _CACHED_CLIENTS
    except ImportError as error:
        return None, f"SFTP benchmark skipped: {error}"

    def make_paramiko_backend():
        return SftpBackend(
            {"allow_agent": False, "look_for_keys": False},
            paramiko.AutoAddPolicy(),
        )

    def make_asyncssh_backend():
        return AsyncsshSftpBackend(
            {
                "config": None,
                "client_keys": None,
                "agent_path": None,
                "public_key_auth": False,
                "kbdint_auth": False,
                "gss_kex": False,
                "gss_auth": False,
                "preferred_auth": "none,password",
            },
            max_concurrency=8,
            sftp_version=4,
        )

    def close_backend(backend, source):
        if type(backend).__name__ == "SftpBackend":
            try:
                client = backend.client(source)
            except Exception:
                client = None
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
                try:
                    client.sock.get_transport().close()
                except Exception:
                    pass
            with _CACHED_CLIENTS.lock:
                _CACHED_CLIENTS.cache.pop((backend, source, threading.get_ident()), None)
            return
        asyncssh_backend_mod._CACHE.invalidate((backend, source))

    rows = []
    scaling_rows = []
    old_asyncssh_timeout = asyncssh_backend_mod._DEFAULT_TIMEOUT
    asyncssh_backend_mod._DEFAULT_TIMEOUT = 3.0
    try:
        with _loopback_sftp_server() as (server_uri, local_root):
            backends = {
                "paramiko": make_paramiko_backend(),
                "asyncssh": make_asyncssh_backend(),
            }
            roots = {
                name: SftpPath(server_uri, backend=backend)
                for name, backend in backends.items()
            }

            static_files = {
                "bench/stat/small.txt": "hello world\n",
                "bench/read/small.txt": "read me\n" * 64,
                "bench/copy_file/source.bin": os.urandom(256 * 1024),
            }
            _write_tree(local_root, static_files)

            list_files = {
                f"bench/listing/file_{i:03d}.txt": f"listing {i}\n"
                for i in range(64)
            }
            list_dirs = {
                f"bench/listing/dir_{i:02d}/nested.txt": "nested\n"
                for i in range(8)
            }
            _write_tree(local_root, {**list_files, **list_dirs})

            walk_tree = {}
            for i in range(4):
                for j in range(5):
                    for k in range(4):
                        walk_tree[
                            f"bench/walk/dir_{i}/sub_{j}/file_{k}.txt"
                        ] = f"{i}-{j}-{k}\n"
            _write_tree(local_root, walk_tree)

            copy_tree = {
                f"bench/copy_tree/source/dir_{i:02d}/file_{j:02d}.txt": ("payload\n" * 32)
                for i in range(4)
                for j in range(4)
            }
            _write_tree(local_root, copy_tree)
            recursive_copy_tree = {
                f"bench/copy_tree_small/source/dir_{i:02d}/file_{j:02d}.txt": ("small\n" * 8)
                for i in range(2)
                for j in range(2)
            }
            _write_tree(local_root, recursive_copy_tree)
            recursive_rm_tree = {
                f"bench/rm_tree/template/dir_{i:02d}/file_{j:02d}.txt": ("rm\n" * 8)
                for i in range(3)
                for j in range(3)
            }
            _write_tree(local_root, recursive_rm_tree)
            for relpath in (
                "bench/write",
                "bench/mkdir",
                "bench/rename",
                "bench/unlink",
                "bench/copy_file",
                "bench/copy_tree",
                "bench/copy_tree_small",
                "bench/rm_tree",
            ):
                (local_root / relpath).mkdir(parents=True, exist_ok=True)
            for backend_name in backends:
                (local_root / "bench/write" / backend_name).mkdir(parents=True, exist_ok=True)
                (local_root / "bench/mkdir" / backend_name).mkdir(parents=True, exist_ok=True)

            try:
                for root in roots.values():
                    _ = root.exists()

                def compare(name, operation_factory, *, repeat=5, warmup=True):
                    timings = {}
                    for backend_name, root in roots.items():
                        timings[backend_name] = _measure(
                            operation_factory(root, backend_name),
                            repeat=repeat,
                            warmup=warmup,
                        )
                    ratio = (
                        timings["paramiko"] / timings["asyncssh"]
                        if timings["asyncssh"] > 0
                        else float("inf")
                    )
                    rows.append((name, timings["paramiko"], timings["asyncssh"], ratio))

                def compare_status(name, operation_factory, *, repeat=1, warmup=False):
                    timings = {}
                    for backend_name, root in roots.items():
                        timings[backend_name] = _measure_status(
                            operation_factory(root, backend_name),
                            repeat=repeat,
                            warmup=warmup,
                        )
                    if all(isinstance(value, (int, float)) for value in timings.values()):
                        ratio = (
                            timings["paramiko"] / timings["asyncssh"]
                            if timings["asyncssh"] > 0
                            else float("inf")
                        )
                    else:
                        ratio = "n/a"
                    rows.append((name, timings["paramiko"], timings["asyncssh"], ratio))

                def make_recursive_rm_operation(root, backend_name):
                    counter = iter(range(1000))

                    def operation():
                        idx = next(counter)
                        target = root / f"bench/rm_tree/{backend_name}_rm_{idx:03d}"
                        (root / "bench/rm_tree/template").copy(
                            target,
                            overwrite=True,
                            recursive=True,
                        )
                        target.rm(recursive=True)

                    return operation

                compare(
                    "warm stat()",
                    lambda root, _backend_name: lambda: (root / "bench/stat/small.txt").stat(),
                    repeat=5,
                )
                compare(
                    "iterdir() 72 entries",
                    lambda root, _backend_name: lambda: list((root / "bench/listing").iterdir()),
                    repeat=3,
                )
                compare(
                    "walk() 80 files",
                    lambda root, _backend_name: lambda: list((root / "bench/walk").walk()),
                    repeat=2,
                )
                compare(
                    "glob('**/*.txt') 80 files",
                    lambda root, _backend_name: lambda: list((root / "bench/walk").glob("**/*.txt")),
                    repeat=2,
                )
                compare(
                    "read_bytes() small file",
                    lambda root, _backend_name: lambda: (root / "bench/read/small.txt").read_bytes(),
                    repeat=5,
                )
                compare(
                    "read_bytes() 64-file batch",
                    lambda root, _backend_name: lambda: [
                        (root / f"bench/listing/file_{i:03d}.txt").read_bytes()
                        for i in range(64)
                    ],
                    repeat=2,
                )
                compare(
                    "stat() 64-file batch",
                    lambda root, _backend_name: lambda: [
                        (root / f"bench/listing/file_{i:03d}.txt").stat()
                        for i in range(64)
                    ],
                    repeat=2,
                )

                compare(
                    "write_bytes() 256 KiB",
                    lambda root, backend_name: (
                        lambda counter=iter(range(1000)): (
                            (
                                root / f"bench/write/{backend_name}/out_{next(counter):03d}.bin"
                            ).write_bytes(b"x" * (256 * 1024))
                        )
                    ),
                    repeat=3,
                    warmup=False,
                )

                compare(
                    "mkdir() leaf dir",
                    lambda root, backend_name: (
                        lambda counter=iter(range(1000)): (
                            (
                                root / f"bench/mkdir/{backend_name}/dir_{next(counter):03d}"
                            ).mkdir(exist_ok=False)
                        )
                    ),
                    repeat=3,
                    warmup=False,
                )

                for backend_name in backends:
                    (local_root / "bench/rename" / backend_name).mkdir(parents=True, exist_ok=True)
                    for i in range(8):
                        (local_root / "bench/rename" / backend_name / f"src_{i:03d}.txt").write_text(
                            "rename\n", encoding="utf-8"
                        )
                compare(
                    "rename() file",
                    lambda root, backend_name: (
                        lambda counter=iter(range(8)): (
                            (lambda i: (
                                root / f"bench/rename/{backend_name}/src_{i:03d}.txt"
                            ).rename(
                                root / f"bench/rename/{backend_name}/dst_{i:03d}.txt"
                            ))(next(counter))
                        )
                    ),
                    repeat=3,
                    warmup=False,
                )

                for backend_name in backends:
                    (local_root / "bench/unlink" / backend_name).mkdir(parents=True, exist_ok=True)
                    for i in range(8):
                        (local_root / "bench/unlink" / backend_name / f"victim_{i:03d}.txt").write_text(
                            "unlink\n", encoding="utf-8"
                        )
                compare(
                    "unlink() file",
                    lambda root, backend_name: (
                        lambda counter=iter(range(8)): (
                            (
                                root / f"bench/unlink/{backend_name}/victim_{next(counter):03d}.txt"
                            ).unlink()
                        )
                    ),
                    repeat=3,
                    warmup=False,
                )

                compare(
                    "copy() single 256 KiB file",
                    lambda root, backend_name: (
                        lambda counter=iter(range(1000)): (
                            (root / "bench/copy_file/source.bin").copy(
                                root / f"bench/copy_file/{backend_name}_out_{next(counter):03d}.bin",
                                overwrite=True,
                            )
                        )
                    ),
                    repeat=3,
                    warmup=False,
                )
                compare_status(
                    "rm(recursive=True) 9-file tree",
                    make_recursive_rm_operation,
                    repeat=1,
                    warmup=False,
                )

                if os.getenv("PATHLIB_NEXT_BENCH_SFTP_RECURSIVE") == "1":
                    compare_status(
                        "copy(recursive=True) 4-file tree",
                        lambda root, backend_name: (
                            lambda counter=iter(range(1000)): (
                                (root / "bench/copy_tree_small/source").copy(
                                    root
                                    / f"bench/copy_tree_small/{backend_name}_out_{next(counter):03d}",
                                    overwrite=True,
                                    recursive=True,
                                )
                            )
                        ),
                        repeat=1,
                        warmup=False,
                    )
                    for max_concurrency in (1, 4):
                        backend = AsyncsshSftpBackend(
                            {
                                "config": None,
                                "client_keys": None,
                                "agent_path": None,
                                "public_key_auth": False,
                                "kbdint_auth": False,
                                "gss_kex": False,
                                "gss_auth": False,
                                "preferred_auth": "none,password",
                            },
                            max_concurrency=max_concurrency,
                            sftp_version=4,
                        )
                        root = SftpPath(server_uri, backend=backend)

                        def scaling_operation(
                            counter=iter(range(1000)),
                            max_concurrency=max_concurrency,
                        ):
                            (root / "bench/copy_tree_small/source").copy(
                                root / f"bench/copy_tree_small/asyncssh_mc{max_concurrency}_{next(counter):03d}",
                                overwrite=True,
                                recursive=True,
                            )

                        metric = _measure_status(
                            scaling_operation,
                            repeat=1,
                            warmup=False,
                        )
                        scaling_rows.append(
                            (f"asyncssh recursive copy mc={max_concurrency}", metric)
                        )
                        try:
                            close_backend(backend, root.source)
                        except Exception:
                            pass

                cold_timings = {}
                for backend_name, backend_factory in (
                    ("paramiko", make_paramiko_backend),
                    ("asyncssh", make_asyncssh_backend),
                ):
                    def cold_operation():
                        backend = backend_factory()
                        path = SftpPath(server_uri, backend=backend) / "bench/stat/small.txt"
                        try:
                            path.stat()
                        finally:
                            close_backend(backend, path.source)

                    cold_timings[backend_name] = _measure(
                        cold_operation, repeat=3, warmup=False
                    )
                rows.append(
                    (
                        "cold connect + stat()",
                        cold_timings["paramiko"],
                        cold_timings["asyncssh"],
                        cold_timings["paramiko"] / cold_timings["asyncssh"],
                    )
                )
            finally:
                for backend_name, backend in backends.items():
                    try:
                        close_backend(backend, roots[backend_name].source)
                    except Exception:
                        pass
    finally:
        asyncssh_backend_mod._DEFAULT_TIMEOUT = old_asyncssh_timeout

    return rows, scaling_rows, "loopback asyncssh server, median seconds"


def main():
    print("Running benchmarks...")
    
    t_uri_parse = benchmark_uri_parse()
    print(f"1. URI Parse (10k runs): {t_uri_parse:.4f}s")

    t_uri_parse_unique = benchmark_uri_parse_unique()
    print(f"1b. URI Parse, unique URIs, forced (us/parse): {t_uri_parse_unique:.2f}us")

    t_uri_parse_compose_unique = benchmark_uri_parse_and_compose_unique()
    print(f"1c. URI Parse+Compose, unique URIs (us/round-trip): {t_uri_parse_compose_unique:.2f}us")

    t_join = benchmark_path_join()
    print(f"2. Path Join (10k runs): {t_join:.4f}s")
    
    t_seg_name = benchmark_segments_name_access()
    print(f"3. Segments/Name Access (10k runs): {t_seg_name:.4f}s")
    
    t_suffix_stem = benchmark_suffix_stem()
    print(f"3b. Suffix/Stem Access (10k runs): {t_suffix_stem:.4f}s")
    
    t_glob_mem = benchmark_glob_mempath()
    print(f"4. Glob over 1k MemPath (20 runs): {t_glob_mem:.4f}s")
    
    t_local, t_std, local_ratio = benchmark_localpath_vs_stdlib()
    print(
        f"5. LocalPath stat (2k runs): {t_local:.4f}s vs pathlib.Path: "
        f"{t_std:.4f}s (Ratio: {local_ratio:.2f}x)"
    )
    local_matrix = benchmark_localpath_matrix()
    print("5b. LocalPath vs pathlib.Path matrix:")
    for name, t_local_case, t_std_case, case_ratio in local_matrix:
        winner = "LocalPath" if case_ratio < 1 else "pathlib.Path"
        print(
            f"   - {name}: LocalPath={t_local_case:.4f}s, "
            f"pathlib.Path={t_std_case:.4f}s "
            f"(LocalPath/pathlib={case_ratio:.2f}x, faster={winner})"
        )
    
    t_http_glob, t_http_walk = benchmark_walk_glob_http()
    print(f"6. HTTP Glob (10 runs): {t_http_glob:.4f}s")
    print(f"7. HTTP Walk (10 runs): {t_http_walk:.4f}s")

    t_parser_pre, t_parser_table = benchmark_http_directory_parser()
    print(f"8. HTTP dir listing parse, Apache <pre>, n=1000 (ms/parse): {t_parser_pre:.4f}ms")
    print(f"9. HTTP dir listing parse, nginx <table>, n=1000 (ms/parse): {t_parser_table:.4f}ms")

    sftp_rows, sftp_scaling_rows, sftp_info = benchmark_sftp_backends()
    if sftp_rows is not None:
        print(f"10. SFTP backend comparison ({sftp_info}):")
        for name, t_paramiko, t_asyncssh, sftp_ratio in sftp_rows:
            if isinstance(t_paramiko, (int, float)) and isinstance(t_asyncssh, (int, float)):
                winner = "asyncssh" if sftp_ratio > 1 else "paramiko"
            else:
                winner = "n/a"
            print(
                f"   - {name}: paramiko={_fmt_metric(t_paramiko)}, "
                f"asyncssh={_fmt_metric(t_asyncssh)} "
                f"(paramiko/asyncssh={_fmt_ratio(sftp_ratio)}, faster={winner})"
            )
        if sftp_scaling_rows:
            print("11. Asyncssh recursive copy scaling:")
            for name, metric in sftp_scaling_rows:
                print(f"   - {name}: {_fmt_metric(metric)}")
    else:
        print(f"10. SFTP backend comparison: {sftp_info}")

    # Print Markdown table format for copy-pasting
    print("\n| Benchmark Case | Time / Metric |")
    print("|---|---|")
    print(f"| URI Parse (10k) | {t_uri_parse:.4f}s |")
    print(f"| URI Parse, unique URIs, forced (us/parse) | {t_uri_parse_unique:.2f}us |")
    print(f"| URI Parse+Compose, unique URIs (us/round-trip) | {t_uri_parse_compose_unique:.2f}us |")
    print(f"| Path Join (10k) | {t_join:.4f}s |")
    print(f"| Segments/Name Access (10k) | {t_seg_name:.4f}s |")
    print(f"| Suffix/Stem Access (10k) | {t_suffix_stem:.4f}s |")
    print(f"| Glob 1k MemPath (20) | {t_glob_mem:.4f}s |")
    print(
        f"| LocalPath vs Stdlib (2k stat) | Local: {t_local:.4f}s, "
        f"Stdlib: {t_std:.4f}s (Ratio: {local_ratio:.2f}x) |"
    )
    for name, t_local_case, t_std_case, case_ratio in local_matrix:
        print(
            f"| LocalPath {name} | Local: {t_local_case:.4f}s, "
            f"Stdlib: {t_std_case:.4f}s (Local/Stdlib: {case_ratio:.2f}x) |"
        )
    print(f"| HTTP Glob (10) | {t_http_glob:.4f}s |")
    print(f"| HTTP Walk (10) | {t_http_walk:.4f}s |")
    print(f"| HTTP dir listing parse, Apache <pre> (n=1000) | {t_parser_pre:.4f}ms/parse |")
    print(f"| HTTP dir listing parse, nginx <table> (n=1000) | {t_parser_table:.4f}ms/parse |")
    if sftp_rows is not None:
        for name, t_paramiko, t_asyncssh, sftp_ratio in sftp_rows:
            print(
                f"| SFTP {name} | paramiko: {_fmt_metric(t_paramiko)}, "
                f"asyncssh: {_fmt_metric(t_asyncssh)} "
                f"(paramiko/asyncssh: {_fmt_ratio(sftp_ratio)}) |"
            )
        for name, metric in sftp_scaling_rows:
            print(f"| SFTP {name} | {_fmt_metric(metric)} |")
    else:
        print(f"| SFTP backend comparison | {sftp_info} |")

if __name__ == "__main__":
    main()
