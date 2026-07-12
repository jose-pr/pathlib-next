from __future__ import annotations

import io
import tarfile
import typing as _ty
import zipfile

if _ty.TYPE_CHECKING:
    from ..path import Path


def _detect_format(name: str, peek: "_ty.Callable[[], bytes] | None" = None) -> str:
    """Detect "zip" vs "tar" from `name`'s extension, falling back to
    magic-byte sniffing (`PK` header -> zip) via `peek()` -- called lazily,
    only when the extension is inconclusive, so callers backed by a remote
    Path don't pay for a round trip in the common case. Shared by
    `unpack_archive` and the `archive:` catch-all URI scheme so both use one
    detection policy."""
    name_lower = name.lower()
    if name_lower.endswith((".zip", ".jar")):
        return "zip"
    if name_lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
        return "tar"
    magic = peek() if peek is not None else b""
    return "zip" if magic.startswith(b"PK") else "tar"


def make_archive(src: Path, format: str, target: Path) -> None:
    """Create an archive file from `src` at `target`.

    Supports format='zip' and format='tar'.
    Operations run stream-first to support any Path implementation.
    """
    if format not in ("zip", "tar"):
        raise ValueError(f"Unsupported format: {format}")

    with target.open("wb") as out_f:
        if format == "zip":
            with zipfile.ZipFile(out_f, "w", zipfile.ZIP_DEFLATED) as archive:
                if src.is_file():
                    with archive.open(src.name, "w") as dest_f:
                        with src.open("rb") as src_f:
                            while True:
                                chunk = src_f.read(65536)
                                if not chunk:
                                    break
                                dest_f.write(chunk)
                else:
                    for dirpath, dirnames, filenames in src.walk():
                        for filename in filenames:
                            file_path = dirpath / filename
                            rel_path = file_path.relative_to(src)
                            arcname = "/".join(rel_path.parts)
                            with archive.open(arcname, "w") as dest_f:
                                with file_path.open("rb") as src_f:
                                    while True:
                                        chunk = src_f.read(65536)
                                        if not chunk:
                                            break
                                        dest_f.write(chunk)
        elif format == "tar":
            with tarfile.open(fileobj=out_f, mode="w") as archive:
                if src.is_file():
                    stat = src.stat()
                    info = tarfile.TarInfo(name=src.name)
                    info.size = stat.st_size
                    info.mode = stat.st_mode
                    with src.open("rb") as src_f:
                        archive.addfile(info, src_f)
                else:
                    for dirpath, dirnames, filenames in src.walk():
                        for filename in filenames:
                            file_path = dirpath / filename
                            rel_path = file_path.relative_to(src)
                            arcname = "/".join(rel_path.parts)
                            stat = file_path.stat()
                            info = tarfile.TarInfo(name=arcname)
                            info.size = stat.st_size
                            info.mode = stat.st_mode
                            with file_path.open("rb") as src_f:
                                archive.addfile(info, src_f)


def unpack_archive(archive: Path, dest: Path) -> None:
    """Extract `archive` file into `dest` directory.

    Supports format detection from filename.
    Operations run stream-first to support any Path implementation.
    """
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)

    def _peek() -> bytes:
        try:
            with archive.open("rb") as f:
                return f.read(4)
        except Exception:
            return b"PK"  # can't sniff -- preserve the historical "assume zip" default

    is_zip = _detect_format(archive.name, _peek) == "zip"

    with archive.open("rb") as in_f:
        if is_zip:
            with zipfile.ZipFile(in_f) as zip_ref:
                for member in zip_ref.infolist():
                    filename = member.filename
                    parts = [p for p in filename.replace("\\", "/").split("/") if p and p != ".."]
                    if not parts:
                        continue

                    target_path = dest
                    for part in parts:
                        target_path = target_path / part

                    if member.is_dir() or filename.endswith("/"):
                        target_path.mkdir(parents=True, exist_ok=True)
                    else:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        with target_path.open("wb") as out_f:
                            with zip_ref.open(member) as member_f:
                                while True:
                                    chunk = member_f.read(65536)
                                    if not chunk:
                                        break
                                    out_f.write(chunk)
        else:
            with tarfile.open(fileobj=in_f, mode="r") as tar_ref:
                for member in tar_ref.getmembers():
                    filename = member.name
                    parts = [p for p in filename.replace("\\", "/").split("/") if p and p != ".."]
                    if not parts:
                        continue

                    target_path = dest
                    for part in parts:
                        target_path = target_path / part

                    if member.isdir():
                        target_path.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        with target_path.open("wb") as out_f:
                            member_f = tar_ref.extractfile(member)
                            if member_f is not None:
                                while True:
                                    chunk = member_f.read(65536)
                                    if not chunk:
                                        break
                                    out_f.write(chunk)
