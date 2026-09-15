from __future__ import annotations

import argparse
import shutil
import sys
import typing as _ty

from .. import LocalPath, UriPath
from ..utils.sync import PathSyncer, SyncEvent

_CHUNK_SIZE = 1024 * 1024


def _looks_like_uri(value: str) -> bool:
    if "://" in value:
        return True
    colon = value.find(":")
    if colon <= 0:
        return False
    slash_positions = [pos for pos in (value.find("/"), value.find("\\")) if pos >= 0]
    first_slash = min(slash_positions) if slash_positions else len(value)
    if colon > first_slash:
        return False
    return not (colon == 1 and value[0].isalpha())


def _path(value: str):
    if _looks_like_uri(value):
        return UriPath(value, findclass=True)
    return LocalPath(value)


def _stdin(stdin):
    return stdin if stdin is not None else sys.stdin.buffer


def _stdout(stdout):
    return stdout if stdout is not None else sys.stdout.buffer


def _copy_stream(source: str, target: str, *, stdin=None, stdout=None) -> None:
    """Copy `source` to `target` in chunks; either may be `-` (stdin or
    stdout). Never holds the whole object in memory."""
    if source == "-":
        reader = _stdin(stdin)
        _write_stream(reader, target, stdout=stdout)
        return
    with _path(source).open("rb") as reader:
        _write_stream(reader, target, stdout=stdout)


def _write_stream(reader, target: str, *, stdout=None) -> None:
    if target == "-":
        writer = _stdout(stdout)
        shutil.copyfileobj(reader, writer, _CHUNK_SIZE)
        writer.flush()
        return
    with _path(target).open("wb") as writer:
        shutil.copyfileobj(reader, writer, _CHUNK_SIZE)


def _cmd_read(args, *, stdin=None, stdout=None) -> int:
    _copy_stream(args.path, "-", stdin=stdin, stdout=stdout)
    return 0


def _cmd_write(args, *, stdin=None, stdout=None) -> int:
    if args.data is None:
        _copy_stream("-", args.path, stdin=stdin, stdout=stdout)
        return 0
    data = args.data.encode(args.encoding)
    if args.path == "-":
        writer = _stdout(stdout)
        writer.write(data)
        writer.flush()
    else:
        _path(args.path).write_bytes(data)
    return 0


def _cmd_rm(args, *, stdin=None, stdout=None) -> int:
    _path(args.path).rm(
        recursive=args.recursive,
        missing_ok=args.missing_ok,
        ignore_error=args.ignore_error,
    )
    return 0


def _cmd_cp(args, *, stdin=None, stdout=None) -> int:
    if args.source == "-" or args.target == "-":
        _copy_stream(args.source, args.target, stdin=stdin, stdout=stdout)
        return 0

    _path(args.source).copy(
        _path(args.target),
        overwrite=args.overwrite,
        follow_symlinks=args.follow_symlinks,
        preserve_metadata=args.preserve_metadata,
        recursive=args.recursive,
    )
    return 0


_SYNC_ACTIONS = {
    SyncEvent.Copy: "copy",
    SyncEvent.RemovedMissing: "remove",
    SyncEvent.CreatedDirectory: "mkdir",
    SyncEvent.TypeMismatch: "replace",
    SyncEvent.Symlink: "symlink",
}


def _cmd_sync(args, *, stdin=None, stdout=None) -> int:
    # The default policy compares content (a native digest, or a streamed
    # md5), so a same-size edit is copied; `--size-only` keeps the cheap
    # size comparison.
    checksum = (lambda entry: entry.stat.st_size) if args.size_only else None
    writer = _stdout(stdout)

    def report(source, target, event, dry_run):
        action = _SYNC_ACTIONS.get(event)
        if action is None or not (dry_run or args.verbose):
            return
        prefix = "would " if dry_run else ""
        if event is SyncEvent.Copy:
            line = f"{prefix}{action} {source.path} -> {target.path}"
        else:
            line = f"{prefix}{action} {target.path}"
        writer.write(line.encode("utf-8", "backslashreplace") + b"\n")

    PathSyncer(
        checksum,
        remove_missing=args.remove_missing,
        follow_symlinks=args.follow_symlinks,
        hook=report,
    ).sync(
        _path(args.source),
        _path(args.target),
        dry_run=args.dry_run,
    )
    writer.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uripath",
        description="Read, write, copy, remove, and sync pathlib_next paths.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    read = subparsers.add_parser("read", help="write PATH bytes to stdout")
    read.add_argument("path")
    read.set_defaults(func=_cmd_read)

    write = subparsers.add_parser("write", help="write stdin or DATA to PATH")
    write.add_argument("path")
    write.add_argument("data", nargs="?")
    write.add_argument("--encoding", default="utf-8")
    write.set_defaults(func=_cmd_write)

    rm = subparsers.add_parser("rm", help="remove PATH")
    rm.add_argument("path")
    rm.add_argument("-r", "--recursive", action="store_true")
    rm.add_argument("--missing-ok", action="store_true")
    rm.add_argument("--ignore-error", action="store_true")
    rm.set_defaults(func=_cmd_rm)

    cp = subparsers.add_parser("cp", help="copy SOURCE to TARGET")
    cp.add_argument("source")
    cp.add_argument("target")
    cp.add_argument("-r", "--recursive", action="store_true")
    cp.add_argument("--overwrite", action="store_true")
    cp.add_argument(
        "--no-follow-symlinks",
        dest="follow_symlinks",
        action="store_false",
        default=True,
    )
    cp.add_argument(
        "--no-preserve-metadata",
        dest="preserve_metadata",
        action="store_false",
        default=True,
    )
    cp.set_defaults(func=_cmd_cp)

    sync = subparsers.add_parser(
        "sync",
        help="sync SOURCE tree to TARGET, comparing file content",
    )
    sync.add_argument("source")
    sync.add_argument("target")
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change without changing anything",
    )
    sync.add_argument("--remove-missing", action="store_true")
    sync.add_argument(
        "--size-only",
        action="store_true",
        help="compare file sizes only (misses same-size edits)",
    )
    sync.add_argument(
        "-v", "--verbose", action="store_true", help="print each change made"
    )
    sync.add_argument(
        "--no-follow-symlinks",
        dest="follow_symlinks",
        action="store_false",
        default=True,
    )
    sync.set_defaults(func=_cmd_sync)
    return parser


def main(
    argv: _ty.Sequence[str] | None = None,
    *,
    stdin=None,
    stdout=None,
    stderr=None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args, stdin=stdin, stdout=stdout)
    except Exception as error:
        stream = stderr if stderr is not None else sys.stderr
        print(f"uripath: {type(error).__name__}: {error}", file=stream)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
