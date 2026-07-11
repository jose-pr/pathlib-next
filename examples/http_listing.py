"""List and read files over HTTP against a real directory index. Requires
the `http` extra (`pip install pathlib_next[http]`) and network access --
guarded under `if __name__ == "__main__"` so importing this module is
always safe, and skippable (prints a message and exits 0) if the target
isn't reachable.

Run directly:

    python examples/http_listing.py
    HTTP_LISTING_URL=http://example.com/some/dir/ python examples/http_listing.py
"""
import os
import sys

from pathlib_next.uri import UriPath

# A stable, classic Apache mod_autoindex directory listing -- htmllistparse
# understands this format. Not every server does (e.g. many modern mirrors
# render a JS-templated index page instead of a plain HTML listing) --
# point HTTP_LISTING_URL at your own server if in doubt.
DEFAULT_URL = "http://ftp.gnu.org/gnu/"


def list_and_stat(url: str):
    root = UriPath(url)
    print(f"Listing {url}")
    for child in root.iterdir():
        kind = "dir " if child.is_dir() else "file"
        size = "" if child.is_dir() else f" ({child.stat().st_size} bytes)"
        print(f"  [{kind}] {child.name}{size}")


if __name__ == "__main__":
    url = os.environ.get("HTTP_LISTING_URL", DEFAULT_URL)
    try:
        list_and_stat(url)
    except Exception as error:
        print(f"Could not reach {url} ({error}); skipping.", file=sys.stderr)
