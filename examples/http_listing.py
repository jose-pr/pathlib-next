"""List and read files over HTTP against a real directory index. Requires
the `http` extra (`pip install pathlib_next[http]`) and network access --
guarded under `if __name__ == "__main__"` so importing this module is
always safe, and skipped (prints setup instructions, exit 0, no request) unless
HTTP_LISTING_URL is set.

The server must render a plain HTML index in the Apache mod_autoindex or
nginx `<table>` style, which the listing parser understands; many modern
mirrors render a JavaScript-templated page instead.

Run directly:

    HTTP_LISTING_URL=http://example.com/some/dir/ python examples/http_listing.py
"""

import os
import sys

from pathlib_next.uri import UriPath


def list_and_stat(root: UriPath):
    # str() of a URI path drops any password in the URL, so printing it is safe.
    print(f"Listing {root}")
    for child in root.iterdir():
        kind = "dir " if child.is_dir() else "file"
        size = "" if child.is_dir() else f" ({child.stat().st_size} bytes)"
        print(f"  [{kind}] {child.name}{size}")


if __name__ == "__main__":
    url = os.environ.get("HTTP_LISTING_URL")
    if not url:
        print(
            "HTTP_LISTING_URL is not set -- skipping. See this file's "
            "module docstring for how to run it.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    root = UriPath(url)
    try:
        list_and_stat(root)
    except Exception as error:
        print(f"Could not reach {root} ({error}); skipping.", file=sys.stderr)
