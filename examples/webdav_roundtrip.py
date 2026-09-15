"""Demonstrates WebDAV read/write operations using DavPath.
Requires a running WebDAV server -- guarded under `if __name__ == "__main__"` so
importing this module is always safe, and skipped (prints setup instructions,
exit 0) unless WEBDAV_EXAMPLE_URL is set.

Run directly:

    export WEBDAV_EXAMPLE_URL=dav://localhost:8080/webdav/
    export WEBDAV_EXAMPLE_USER=user         # optional
    export WEBDAV_EXAMPLE_PASSWORD=pass     # optional
    python examples/webdav_roundtrip.py
"""

import os
import sys
from urllib.parse import quote, urlsplit, urlunsplit

from pathlib_next.uri import UriPath


def webdav_roundtrip(root: UriPath):
    # str() of a URI path drops the password, so printing it is safe.
    print(f"Connecting to WebDAV: {root}")

    # 1. Create a directory
    test_dir = root / "pathlib_next_test"
    print(f"Creating directory: {test_dir}")
    test_dir.mkdir(exist_ok=True)

    # 2. Write a file
    test_file = test_dir / "test.txt"
    print(f"Writing file: {test_file}")
    test_file.write_text("Hello from WebDAV!")

    # 3. List the directory contents
    print("Listing directory:")
    for child in test_dir.iterdir():
        print(f"  - {child.name} (size: {child.stat().st_size} bytes)")

    # 4. Read the file back
    print("Reading file content:", test_file.read_text())

    # 5. Clean up
    print("Cleaning up (recursive delete)...")
    test_dir.rm(recursive=True)
    print("Cleanup completed.")


if __name__ == "__main__":
    url = os.environ.get("WEBDAV_EXAMPLE_URL")
    if not url:
        print(
            "WEBDAV_EXAMPLE_URL is not set -- skipping. See this file's "
            "module docstring for the required environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    user = os.environ.get("WEBDAV_EXAMPLE_USER")
    password = os.environ.get("WEBDAV_EXAMPLE_PASSWORD")

    # Inject the credentials unless the URL already carries some.
    # Percent-encode both parts: a raw "/", "#", "?" or "@" in a password
    # would otherwise change where the URI's host and path begin.
    parts = urlsplit(url)
    if user and "@" not in parts.netloc:
        userinfo = quote(user, safe="")
        if password:
            userinfo += ":" + quote(password, safe="")
        netloc = f"{userinfo}@{parts.netloc}"
        url = urlunsplit(
            (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
        )
    root = UriPath(url)

    try:
        webdav_roundtrip(root)
    except Exception as error:
        print(
            f"Could not connect to WebDAV at {root} ({error}); skipping.",
            file=sys.stderr,
        )
