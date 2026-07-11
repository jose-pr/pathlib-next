"""Examples demonstrating the offline-runnable URI schemes: data:, zip:, and tar:.
Runs fully offline (no external network or credentials needed).

Run directly:

    python examples/data_and_archive.py
"""
import tarfile
import tempfile
import zipfile

from pathlib_next import Path
from pathlib_next.uri import UriPath


def data_uri_example():
    print("--- data: URI Example ---")
    # Read plain text data URI
    text_uri = UriPath("data:text/plain;charset=utf-8,Hello%20World%21")
    print("DataUri content:", text_uri.read_text())
    print("DataUri media type:", text_uri.mediatype)
    print("DataUri size:", text_uri.stat().st_size)

    # Read base64-encoded binary/text data URI
    b64_uri = UriPath("data:text/plain;base64,SGVsbG8gRnJvbSBCYXNlNjQh")
    print("DataUri base64 content:", b64_uri.read_text())
    print()


def zip_archive_example(tmpdir_path: Path):
    print("--- zip: Archive Example ---")
    # Create a local zip file
    zip_file = tmpdir_path / "archive.zip"
    with zipfile.ZipFile(str(zip_file), "w") as zf:
        zf.writestr("hello.txt", "Hello from ZIP!")
        zf.writestr("sub/nested.txt", "Nested content.")

    # Access the zip entries using pathlib_next
    # Format: zip:file:///<absolute_path>!/<inner_path>
    zip_root_uri = f"zip:{zip_file.as_uri()}!/"
    zip_root = UriPath(zip_root_uri)
    
    print("ZIP listing:", sorted(p.name for p in zip_root.iterdir()))
    
    # Read inner file
    hello_file = zip_root / "hello.txt"
    print("hello.txt inside ZIP:", hello_file.read_text())

    nested_file = zip_root / "sub" / "nested.txt"
    print("sub/nested.txt inside ZIP:", nested_file.read_text())
    
    # Appending a new file to ZIP via the unified path API
    # Writing only works when the archive is a local file URI
    new_member = zip_root / "new_file.txt"
    new_member.write_text("Dynamically written to ZIP!")
    
    # Verify the write by list and read
    print("ZIP listing after write:", sorted(p.name for p in zip_root.iterdir()))
    print("new_file.txt content:", new_member.read_text())
    print()


def tar_archive_example(tmpdir_path: Path):
    print("--- tar: Archive Example ---")
    # Create a local tar file
    tar_file = tmpdir_path / "archive.tar"
    with tarfile.open(str(tar_file), "w") as tf:
        info1 = tarfile.TarInfo(name="readme.txt")
        content1 = b"Welcome to the TAR file."
        info1.size = len(content1)
        tf.addfile(info1, tarfile.io.BytesIO(content1))

        info2 = tarfile.TarInfo(name="docs/index.html")
        content2 = b"<h1>Index</h1>"
        info2.size = len(content2)
        tf.addfile(info2, tarfile.io.BytesIO(content2))

    # Access the tar entries using pathlib_next
    tar_root_uri = f"tar:{tar_file.as_uri()}!/"
    tar_root = UriPath(tar_root_uri)

    print("TAR listing:", sorted(p.name for p in tar_root.iterdir()))
    
    readme = tar_root / "readme.txt"
    print("readme.txt inside TAR:", readme.read_text())

    docs_dir = tar_root / "docs"
    print("docs/ listing:", sorted(p.name for p in docs_dir.iterdir()))
    print()


if __name__ == "__main__":
    data_uri_example()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_archive_example(tmp_path)
        tar_archive_example(tmp_path)
