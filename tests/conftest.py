import functools
import http.server
import threading

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
