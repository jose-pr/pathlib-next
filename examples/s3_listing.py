"""List keys in an S3 bucket using S3Path.
Requires `boto3` and the `s3` extra (`pip install pathlib_next[s3]`), as well
as valid AWS credentials configured (via environment variables, AWS CLI,
or IAM role) -- guarded under `if __name__ == "__main__"` so importing this
module is always safe, and skipped (prints setup instructions, exit 0) unless
S3_EXAMPLE_BUCKET is set.

Run directly:

    export S3_EXAMPLE_BUCKET=my-s3-bucket
    export S3_EXAMPLE_PREFIX=some/folder/   # optional
    python examples/s3_listing.py
"""
import os
import sys

from pathlib_next.uri import UriPath


def list_s3(bucket: str, prefix: str):
    # s3://bucket/prefix/path
    s3_uri = f"s3://{bucket}/{prefix.lstrip('/')}"
    root = UriPath(s3_uri)
    print(f"Listing S3 location: {s3_uri}")
    for child in root.iterdir():
        kind = "dir " if child.is_dir() else "file"
        size = "" if child.is_dir() else f" ({child.stat().st_size} bytes)"
        print(f"  [{kind}] {child.name}{size}")


if __name__ == "__main__":
    bucket = os.environ.get("S3_EXAMPLE_BUCKET")
    if not bucket:
        print(
            "S3_EXAMPLE_BUCKET is not set -- skipping. See this file's "
            "module docstring for the required environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    prefix = os.environ.get("S3_EXAMPLE_PREFIX", "")

    try:
        list_s3(bucket, prefix)
    except Exception as error:
        print(f"Could not connect to S3 bucket {bucket} ({error}); skipping.", file=sys.stderr)
