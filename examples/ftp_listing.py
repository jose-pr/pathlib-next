"""List files on a remote FTP server using FtpPath.
Requires credentials -- guarded under `if __name__ == "__main__"` so importing
this module is always safe, and skipped (prints setup instructions, exit 0) unless
FTP_EXAMPLE_HOST is set.

Run directly:

    export FTP_EXAMPLE_HOST=ftp.debian.org
    export FTP_EXAMPLE_USER=anonymous       # optional
    export FTP_EXAMPLE_PASSWORD=guest       # optional
    export FTP_EXAMPLE_REMOTE_PATH=/debian  # optional, default shown below
    python examples/ftp_listing.py
"""

import os
import sys
from urllib.parse import quote

from pathlib_next.uri import UriPath


def list_ftp(remote: UriPath):
    # str() of a URI path drops the password, so printing it is safe.
    print(f"Listing FTP directory: {remote}")
    for child in remote.iterdir():
        kind = "dir " if child.is_dir() else "file"
        size = "" if child.is_dir() else f" ({child.stat().st_size} bytes)"
        print(f"  [{kind}] {child.name}{size}")


def _userinfo(user, password):
    # Percent-encode both parts: a raw "/", "#", "?" or "@" in a password
    # would otherwise change where the URI's host and path begin.
    if not user:
        return ""
    if password:
        return f"{quote(user, safe='')}:{quote(password, safe='')}@"
    return f"{quote(user, safe='')}@"


if __name__ == "__main__":
    host = os.environ.get("FTP_EXAMPLE_HOST")
    if not host:
        print(
            "FTP_EXAMPLE_HOST is not set -- skipping. See this file's "
            "module docstring for the required environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    user = os.environ.get("FTP_EXAMPLE_USER")
    password = os.environ.get("FTP_EXAMPLE_PASSWORD")
    remote_path = os.environ.get("FTP_EXAMPLE_REMOTE_PATH", "/")

    userinfo = _userinfo(user, password)
    remote = UriPath(f"ftp://{userinfo}{host}{remote_path}")

    try:
        list_ftp(remote)
    except Exception as error:
        print(f"Could not connect to {remote} ({error}); skipping.", file=sys.stderr)
