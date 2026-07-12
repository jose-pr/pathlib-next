import sys
import timeit
import tempfile
import shutil
import functools
import http.server
import threading
from pathlib import Path as StdlibPath

# Add src to sys.path so we can import pathlib_next without installing it
import sys
from pathlib import Path as StdlibPath
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
    # This one forces the actual parse (uri_parse_perf.md's one-pass
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
    # uri_parse_perf.md Phase 1 (_parse_uri) and Phase 2
    # (_format_parsed_parts) together, which is what "Uri() construction"
    # means for that plan's own done-when bar.
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
        
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(tmp_path)
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
    
    t_local, t_std, ratio = benchmark_localpath_vs_stdlib()
    print(f"5. LocalPath stat (2k runs): {t_local:.4f}s vs pathlib.Path: {t_std:.4f}s (Ratio: {ratio:.2f}x)")
    
    t_http_glob, t_http_walk = benchmark_walk_glob_http()
    print(f"6. HTTP Glob (10 runs): {t_http_glob:.4f}s")
    print(f"7. HTTP Walk (10 runs): {t_http_walk:.4f}s")
    
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
    print(f"| LocalPath vs Stdlib (2k stat) | Local: {t_local:.4f}s, Stdlib: {t_std:.4f}s (Ratio: {ratio:.2f}x) |")
    print(f"| HTTP Glob (10) | {t_http_glob:.4f}s |")
    print(f"| HTTP Walk (10) | {t_http_walk:.4f}s |")

if __name__ == "__main__":
    main()
