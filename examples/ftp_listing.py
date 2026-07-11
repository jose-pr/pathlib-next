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

from pathlib_next.uri import UriPath


def list_ftp(remote_uri: str):
    remote = UriPath(remote_uri)
    print(f"Listing FTP directory: {remote_uri}")
    for child in remote.iterdir():
        kind = "dir " if child.is_dir() else "file"
        size = "" if child.is_dir() else f" ({child.stat().st_size} bytes)"
        print(f"  [{kind}] {child.name}{size}")


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
    
    userinfo = f"{user}:{password}@" if user and password else (f"{user}@" if user else "")
    remote_uri = f"ftp://{userinfo}{host}{remote_path}"

    try:
        list_ftp(remote_uri)
    except Exception as error:
        print(f"Could not connect to {remote_uri} ({error}); skipping.", file=sys.stderr)
