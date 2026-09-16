from __future__ import annotations

import shutil
import tarfile
import tempfile
import typing as _ty
import warnings
import zipfile

from . import is_safe_child_name, is_windows_flavoured
from .stat import FileStat

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


def _safe_member_parts(name: str, *, windows: bool) -> "list[str] | None":
    """Split archive member `name` into the parts to join onto the
    extraction directory, or return None when the member must be skipped
    because a part would leave it (`..`, and with `windows=True` a drive
    such as `D:x` or the drive-relative `C:..`). `/` always splits; a
    backslash splits only for a Windows destination, the only place it means
    "separator" -- on POSIX it is an ordinary filename character, and
    splitting it unconditionally turned one legitimate member into a
    directory plus a file on every platform. Empty and `.` parts are
    dropped, so `/abs` and `./x` stay inside."""
    if windows:
        name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or not all(is_safe_child_name(p, windows=windows) for p in parts):
        return None
    return parts


#: In-memory size up to which an archive being built or read is buffered
#: before spilling to a temporary file.
_SPOOL_SIZE = 64 * 1024 * 1024


class _Spool(tempfile.SpooledTemporaryFile):
    """A seekable in-memory-then-disk buffer. `SpooledTemporaryFile` only
    gained `seekable()`/`readable()`/`writable()` in 3.11, and zipfile
    probes them."""

    def __init__(self):
        super().__init__(max_size=_SPOOL_SIZE)

    def seekable(self):
        return True

    def readable(self):
        return True

    def writable(self):
        return True


def _archive_members(src: "Path") -> "_ty.Iterator[tuple[Path, str]]":
    """Yield (file, member name) for every non-directory entry below the
    directory `src`. The name is built from the listed names while
    descending, so it works for any `Path` (no `relative_to()`/`parts`,
    which MemPath and UriPath do not provide in that form). Directories are
    recognised without following symlinks, as `walk()` does."""
    pending = [(src, "")]
    while pending:
        directory, prefix = pending.pop()
        subdirs = []
        for child in directory.iterdir():
            name = f"{prefix}{child.name}"
            try:
                stat = FileStat.from_path(child, follow_symlink=False)
                is_dir = stat is not None and stat.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                subdirs.append((child, f"{name}/"))
            else:
                yield child, name
        pending.extend(reversed(subdirs))


def _copy_chunks(src_f, dest_f) -> None:
    while True:
        chunk = src_f.read(65536)
        if not chunk:
            break
        dest_f.write(chunk)


def make_archive(src: Path, format: str, target: Path) -> None:
    """Create an archive file from `src` at `target`.

    Supports format='zip' and format='tar'.
    Operations run stream-first to support any Path implementation, for
    `src` as well as `target`. The archive is built in a temporary buffer
    and written to `target` only once complete, so a failure leaves an
    existing `target` untouched. Zip members always carry zip64 headers,
    so a member over 2 GiB is stored instead of failing after the fact.
    """
    if format not in ("zip", "tar"):
        raise ValueError(f"Unsupported format: {format}")

    if src.is_file():
        members = [(src, src.name)]
    elif src.is_dir():
        members = _archive_members(src)
    else:
        raise FileNotFoundError(f"No such file or directory: {str(src)!r}")

    with _Spool() as buffer:
        if format == "zip":
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for file_path, arcname in members:
                    # force_zip64: the size is unknown when the entry opens,
                    # and without it zipfile raises once 2 GiB were written.
                    with archive.open(arcname, "w", force_zip64=True) as dest_f:
                        with file_path.open("rb") as src_f:
                            _copy_chunks(src_f, dest_f)
        else:
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for file_path, arcname in members:
                    stat = file_path.stat()
                    info = tarfile.TarInfo(name=arcname)
                    info.size = stat.st_size
                    info.mode = stat.st_mode
                    with file_path.open("rb") as src_f:
                        archive.addfile(info, src_f)
        buffer.seek(0)
        with target.open("wb") as out_f:
            shutil.copyfileobj(buffer, out_f)


def unpack_archive(archive: Path, dest: Path) -> None:
    """Extract `archive` file into `dest` directory.

    Supports format detection from filename.
    Operations run stream-first to support any Path implementation.
    Members whose name would land outside `dest` (a `..` part, or on a
    Windows-flavoured `dest` a drive such as `D:x` or `C:..`) are skipped.
    A non-seekable archive stream (e.g. `HttpPath`) is buffered first.
    A tar hard link, or a symlink to a regular file inside the archive, is
    extracted as a regular file with the target member's content (no link
    is created, so `dest` needs no symlink support); a link that does not
    resolve to such a member is skipped with a `UserWarning`.
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
    windows = is_windows_flavoured(dest)

    with archive.open("rb") as raw_in, _Spool() as spool:
        in_f = raw_in
        seekable = getattr(raw_in, "seekable", None)
        if seekable is None or not seekable():
            # zipfile seeks to the central directory, and tarfile's "r:*"
            # probe seeks back: neither works over a network stream.
            shutil.copyfileobj(raw_in, spool)
            spool.seek(0)
            in_f = spool
        if is_zip:
            with zipfile.ZipFile(in_f) as zip_ref:
                for member in zip_ref.infolist():
                    filename = member.filename
                    parts = _safe_member_parts(filename, windows=windows)
                    if parts is None:
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
                                _copy_chunks(member_f, out_f)
        else:
            with tarfile.open(fileobj=in_f, mode="r") as tar_ref:
                for member in tar_ref.getmembers():
                    filename = member.name
                    parts = _safe_member_parts(filename, windows=windows)
                    if parts is None:
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
                                _copy_chunks(member_f, out_f)
                    elif member.islnk() or member.issym():
                        # extractfile() resolves a link to the member it
                        # names inside the archive -- never the filesystem.
                        try:
                            member_f = tar_ref.extractfile(member)
                        except KeyError:
                            member_f = None
                        if member_f is None:
                            warnings.warn(
                                f"unpack_archive: skipped link {filename!r} -> "
                                f"{member.linkname!r}: not a regular file in "
                                "the archive",
                                UserWarning,
                                stacklevel=2,
                            )
                            continue
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        with member_f, target_path.open("wb") as out_f:
                            _copy_chunks(member_f, out_f)
