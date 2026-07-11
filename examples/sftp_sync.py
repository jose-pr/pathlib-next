"""Sync a directory tree from a remote SFTP server to the local
filesystem using PathSyncer. Requires the `sftp` extra
(`pip install pathlib_next[sftp]`), a real SSH server, and credentials --
guarded under `if __name__ == "__main__"` so importing this module is
always safe, and skipped (prints setup instructions, exit 0) unless
SFTP_EXAMPLE_HOST is set.

Run directly:

    export SFTP_EXAMPLE_HOST=myhost
    export SFTP_EXAMPLE_USER=myuser        # optional
    export SFTP_EXAMPLE_PASSWORD=mypass    # optional
    export SFTP_EXAMPLE_REMOTE_PATH=/var/log   # optional, default shown below
    python examples/sftp_sync.py
"""
import os
import sys
import tempfile

from pathlib_next import Path
from pathlib_next.uri import UriPath
from pathlib_next.utils.sync import PathAndStat, PathSyncer


def checksum(entry: PathAndStat):
    return entry.stat.st_size


def sync_from_sftp(remote_uri: str, local_root: Path):
    remote = UriPath(remote_uri)
    print(f"Syncing {remote_uri} -> {local_root}")
    syncer = PathSyncer(checksum, remove_missing=True)
    syncer.sync(remote, local_root, dry_run=False)
    print("Synced:", sorted(p.name for p in local_root.iterdir()))


if __name__ == "__main__":
    host = os.environ.get("SFTP_EXAMPLE_HOST")
    if not host:
        print(
            "SFTP_EXAMPLE_HOST is not set -- skipping. See this file's "
            "module docstring for the required environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    user = os.environ.get("SFTP_EXAMPLE_USER")
    password = os.environ.get("SFTP_EXAMPLE_PASSWORD")
    remote_path = os.environ.get("SFTP_EXAMPLE_REMOTE_PATH", "/var/log")
    userinfo = f"{user}:{password}@" if user and password else (f"{user}@" if user else "")
    remote_uri = f"sftp://{userinfo}{host}{remote_path}"

    with tempfile.TemporaryDirectory() as tmp:
        try:
            sync_from_sftp(remote_uri, Path(tmp))
        except Exception as error:
            print(f"Could not sync from {remote_uri} ({error}); skipping.", file=sys.stderr)
