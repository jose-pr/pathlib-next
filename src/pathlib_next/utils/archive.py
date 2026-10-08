from __future__ import annotations

import posixpath
import shutil
import tarfile
import tempfile
import time
import typing as _ty
import warnings
import zipfile

from . import _is_safe_name, is_windows_flavoured
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


def _safe_member_parts(
    name: str, *, windows: bool, rewrites: bool = True
) -> "list[str] | None":
    """Split archive member `name` into the parts to join onto the
    extraction directory, or return None when the member must be skipped
    because a part would leave it (`..`, and with `windows=True` a drive
    such as `D:x` or the drive-relative `C:..`) or, with `windows=True` and
    `rewrites`, because Windows would store a part under another name or send
    it to a device (`nul`, `trail.`). `/` always splits; a backslash splits
    only for a Windows destination, the only place it means "separator" -- on
    POSIX it is an ordinary filename character, and splitting it
    unconditionally turned one legitimate member into a directory plus a file
    on every platform. Empty and `.` parts are dropped, so `/abs` and `./x`
    stay inside."""
    if windows:
        name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or not all(_is_safe_name(p, windows, rewrites) for p in parts):
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


def _archive_members(
    src: "Path",
) -> "_ty.Iterator[tuple[Path, str, FileStat | None, bool]]":
    """Yield `(path, member name, listed stat, is a directory)` for every
    entry below the directory `src`, each directory before what is in it and
    the empty ones too. The name is built from the listed names while
    descending, so it works for any `Path` (no `relative_to()`/`parts`,
    which MemPath and UriPath do not provide in that form). Directories are
    recognised without following symlinks, as `walk()` does; the stat is the
    one that decided that, or None when it failed."""
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
                stat, is_dir = None, False
            if is_dir:
                yield child, name, stat, True
                subdirs.append((child, f"{name}/"))
            else:
                yield child, name, stat, False
        pending.extend(reversed(subdirs))


def _content_stat(path: "Path", listed: "FileStat | None") -> "FileStat | None":
    """The stat that describes what `path` holds: the listing's own for
    anything but a link, else what the link resolves to. None when the backend
    has none to give: a member then carries no time and a default mode."""
    if listed is not None and not listed.is_symlink():
        return listed
    try:
        return FileStat.from_path(path)
    except (OSError, NotImplementedError):
        return None


#: The earliest date a zip member can carry; also the date of one whose source
#: reports no modification time.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _zip_date(stat: "FileStat | None") -> tuple:
    mtime = getattr(stat, "st_mtime", 0)
    if mtime:
        try:
            date = time.localtime(mtime)[:6]
        except (OverflowError, OSError, ValueError):
            return _ZIP_EPOCH
        if 1980 <= date[0] <= 2107:
            return date
    return _ZIP_EPOCH


def _tar_info(
    name: str, stat: "FileStat | None", *, directory: bool
) -> tarfile.TarInfo:
    """A member header carrying the source's modification time and permission
    bits. A source that reports no mode (`mode_known` false, or no stat) gets
    the usual 0o644 for a file and 0o755 for a directory, not the read-only
    placeholder a backend invents."""
    info = tarfile.TarInfo(name)
    if directory:
        info.type = tarfile.DIRTYPE
    if stat is not None and getattr(stat, "mode_known", True) and stat.st_mode:
        info.mode = stat.st_mode & 0o7777
    else:
        info.mode = 0o755 if directory else 0o644
    info.mtime = int(getattr(stat, "st_mtime", 0) or 0)
    return info


def _copy_chunks(src_f, dest_f) -> int:
    """Copy `src_f` to `dest_f` until the end; the number of bytes copied."""
    total = 0
    while True:
        chunk = src_f.read(65536)
        if not chunk:
            break
        dest_f.write(chunk)
        total += len(chunk)
    return total


class _TarLinks:
    """Where the members of one tar archive are by name, for resolving its
    links. A symlink is read relative to its own directory and finds the last
    member of that name; a hard link finds the last one before it."""

    def __init__(self, members: "list[tarfile.TarInfo]") -> None:
        self._position = {}
        self._named: "dict[str, list[int]]" = {}
        self._members = members
        for index, member in enumerate(members):
            self._position[id(member)] = index
            self._named.setdefault(posixpath.normpath(member.name), []).append(index)

    def target(self, link: tarfile.TarInfo) -> "tarfile.TarInfo | None":
        """The regular-file member `link` names, through any chain of links,
        or None when it names nothing, a directory or another non-file, or
        leads back to a link already followed."""
        seen = set()
        current = link
        while current.islnk() or current.issym():
            if id(current) in seen:
                return None
            seen.add(id(current))
            if current.issym():
                name = "/".join(
                    filter(None, (posixpath.dirname(current.name), current.linkname))
                )
                limit = len(self._members)
            else:
                name = current.linkname
                limit = self._position.get(id(current), 0)
            before = [
                index
                for index in self._named.get(posixpath.normpath(name), ())
                if index < limit
            ]
            if not before:
                return None
            current = self._members[before[-1]]
        return current if current.isreg() else None


def make_archive(src: Path, format: str, target: Path) -> None:
    """Create an archive file from `src` at `target`.

    Supports format='zip' and format='tar'.
    Operations run stream-first to support any Path implementation, for
    `src` as well as `target`. The archive is built in a temporary buffer
    and written to `target` only once complete, so a failure leaves an
    existing `target` untouched. Zip members always carry zip64 headers,
    so a member over 2 GiB is stored instead of failing after the fact.

    A directory is a member of its own, empty ones included. A member carries
    the modification time its source reports (a zip date from 1980, a tar time
    in whole seconds; none reported gives 1980-01-01 or the epoch) and, in a
    tar, the permission bits it reports (0o644 for a file and 0o755 for a
    directory when it reports none). A tar member's size is the number of
    bytes read from the source, not what `stat()` says: a backend may not know
    it, or count it differently.
    """
    if format not in ("zip", "tar"):
        raise ValueError(f"Unsupported format: {format}")

    if src.is_file():
        members = [(src, src.name, None, False)]
    elif src.is_dir():
        members = _archive_members(src)
    else:
        raise FileNotFoundError(f"No such file or directory: {str(src)!r}")

    with _Spool() as buffer:
        if format == "zip":
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for file_path, arcname, listed, is_dir in members:
                    stat = listed if is_dir else _content_stat(file_path, listed)
                    if is_dir:
                        info = zipfile.ZipInfo(f"{arcname}/", _zip_date(stat))
                        # drwxr-xr-x, with the MS-DOS directory flag.
                        info.external_attr = (0o40755 << 16) | 0x10
                        archive.writestr(info, b"")
                        continue
                    info = zipfile.ZipInfo(arcname, _zip_date(stat))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    # force_zip64: the size is unknown when the entry opens,
                    # and without it zipfile raises once 2 GiB were written.
                    with archive.open(info, "w", force_zip64=True) as dest_f:
                        with file_path.open("rb") as src_f:
                            _copy_chunks(src_f, dest_f)
        else:
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for file_path, arcname, listed, is_dir in members:
                    if is_dir:
                        archive.addfile(_tar_info(arcname, listed, directory=True))
                        continue
                    info = _tar_info(
                        arcname, _content_stat(file_path, listed), directory=False
                    )
                    # The header comes first and holds the size, so the
                    # content is read once into a buffer to count it.
                    with _Spool() as content:
                        with file_path.open("rb") as src_f:
                            info.size = _copy_chunks(src_f, content)
                        content.seek(0)
                        archive.addfile(info, content)
        buffer.seek(0)
        with target.open("wb") as out_f:
            shutil.copyfileobj(buffer, out_f)


def unpack_archive(archive: Path, dest: Path) -> None:
    """Extract `archive` file into `dest` directory.

    Supports format detection from filename.
    Operations run stream-first to support any Path implementation.
    Members whose name would land outside `dest` (a `..` part, or on a
    Windows-flavoured `dest` a drive such as `D:x` or `C:..`) are skipped.
    On a Windows-flavoured `dest` so is a member with a part Windows would
    store under another name (a trailing dot or space) or send to a device
    (`NUL`, `CON`, `COM1`, ...), with a `UserWarning` naming it.
    A non-seekable archive stream (e.g. `HttpPath`) is buffered first.
    A tar hard link, or a symlink to a regular file inside the archive, is
    extracted as a regular file with the target member's content (no link
    is created, so `dest` needs no symlink support); a link that does not
    resolve to such a member (it names nothing, a directory, or itself
    through other links) is skipped with a `UserWarning`.
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

    def member_parts(filename: str) -> "list[str] | None":
        parts = _safe_member_parts(filename, windows=windows)
        if (
            parts is None
            and windows
            and _safe_member_parts(filename, windows=True, rewrites=False) is not None
        ):
            warnings.warn(
                f"unpack_archive: skipped member {filename!r}: Windows would "
                "store it under another name or send it to a device",
                UserWarning,
                stacklevel=3,
            )
        return parts

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
                    parts = member_parts(filename)
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
                members = tar_ref.getmembers()
                links = None
                for member in members:
                    filename = member.name
                    parts = member_parts(filename)
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
                        # The member a link names inside the archive, never
                        # the filesystem. Resolved here, one hop at a time:
                        # `extractfile()` follows a chain by recursion and
                        # a link that names itself never ends.
                        if links is None:
                            links = _TarLinks(members)
                        target_member = links.target(member)
                        member_f = (
                            None
                            if target_member is None
                            else tar_ref.extractfile(target_member)
                        )
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
