#!/usr/bin/env python3
"""
Example: list contents of a Google Cloud Storage bucket.

Usage:
    GS_EXAMPLE_BUCKET=my-bucket python examples/gs_listing.py

Lists the root directory of a GCS bucket. Requires:
    - google-cloud-storage installed (`pip install pathlib_next[gs]`)
    - Google Cloud credentials (Application Default Credentials or env setup)
"""
import os
from pathlib_next.uri import UriPath


def main():
    bucket = os.environ.get("GS_EXAMPLE_BUCKET")
    if not bucket:
        print(
            "GS_EXAMPLE_BUCKET not set; skipping example.\n"
            "To run: GS_EXAMPLE_BUCKET=<your-bucket> python examples/gs_listing.py"
        )
        return

    try:
        root = UriPath(f"gs://{bucket}/")
        print(f"Listing {root}:")
        for child in root.iterdir():
            print(f"  {child.name}: {'dir' if child.is_dir() else f'{child.stat().st_size} bytes'}")
    except Exception as e:
        print(f"Error: {e}")
        print("Make sure you have GCS credentials set up and the bucket exists.")


if __name__ == "__main__":
    main()
