#!/usr/bin/env python3
"""
Example: list contents of an Azure Blob Storage container.

Usage:
    AZ_EXAMPLE_ACCOUNT=myaccount AZ_EXAMPLE_CONTAINER=mycontainer python examples/az_listing.py

Lists the root directory of an Azure Blob Storage container. Requires:
    - azure-storage-blob installed (`pip install pathlib_next[az]`)
    - Azure credentials (DefaultAzureCredential or connection string env setup)
"""
import os
from pathlib_next.uri import UriPath


def main():
    account = os.environ.get("AZ_EXAMPLE_ACCOUNT")
    container = os.environ.get("AZ_EXAMPLE_CONTAINER")
    if not account or not container:
        print(
            "AZ_EXAMPLE_ACCOUNT and/or AZ_EXAMPLE_CONTAINER not set; skipping example.\n"
            "To run: AZ_EXAMPLE_ACCOUNT=<account> AZ_EXAMPLE_CONTAINER=<container> python examples/az_listing.py"
        )
        return

    try:
        root = UriPath(f"az://{account}/{container}/")
        print(f"Listing {root}:")
        for child in root.iterdir():
            print(f"  {child.name}: {'dir' if child.is_dir() else f'{child.stat().st_size} bytes'}")
    except Exception as e:
        print(f"Error: {e}")
        print("Make sure you have Azure credentials set up and the container exists.")


if __name__ == "__main__":
    main()
