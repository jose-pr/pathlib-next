from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import typing as _ty

from .. import LocalPath
from ..utils.sync import PathSyncer, SyncEvent

try:
    from ..uri import Source, UriPath
except ImportError as _error:
    # The `uri` extra is optional: local paths and `-` still work without
    # it, and a URI argument reports what to install (see `_path()`).
    UriPath = None
    _URI_IMPORT_ERROR = _error
else:
    _URI_IMPORT_ERROR = None

_CHUNK_SIZE = 1024 * 1024


# RFC 3986 scheme, then the colon.
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")

#: Exit status after the reader of stdout went away (128 + SIGPIPE), as a
#: POSIX tool killed by SIGPIPE reports it; and after Ctrl-C (128 + SIGINT).
_EXIT_BROKEN_PIPE = 141
_EXIT_INTERRUPTED = 130


def _looks_like_uri(value: str) -> bool:
    """Whether a command-line argument is a URI rather than a local path.
    Needs an RFC 3986 scheme prefix; without `://` the scheme must also be
    one a class registers (`data:`, `zip:`, ...), so POSIX file names such
    as `12:30.txt` or `notes:draft` stay local paths. `./name` always is."""
    match = _SCHEME_RE.match(value)
    if match is None:
        return False
    if match.end() == 2:
        # A drive letter (`C:/x`, `C:x`), not a one-letter scheme.
        return False
    if "://" in value:
        return True
    if UriPath is None:
        # Cannot tell which schemes exist; `_path()` names the extra.
        return True
    scheme = value[: match.end() - 1].lower()
    try:
        return Source(scheme, None, None, None).get_scheme_cls() is not UriPath
    except Exception:
        # A scheme plugin that fails to load: `_path()` reports it.
        return True


def _path(value: str):
    if _looks_like_uri(value):
        if UriPath is None:
            raise ImportError(
                f"{value!r} is a URI, which needs the 'uri' extra:"
                " pip install 'pathlib-next[uri]'"
            ) from _URI_IMPORT_ERROR
        return UriPath(value, findclass=True)
    return LocalPath(value)


def _stdin(stdin):
    return stdin if stdin is not None else sys.stdin.buffer


class _StdoutClosed(Exception):
    """The reader of stdout went away (`uripath read big | head`)."""


class _StdoutWriter:
    """Writes to stdout, turning a BrokenPipeError there -- and only there,
    not one from a remote connection -- into `_StdoutClosed`."""

    __slots__ = ("_raw",)

    def __init__(self, raw):
        self._raw = raw

    def write(self, data):
        try:
            return self._raw.write(data)
        except BrokenPipeError as error:
            raise _StdoutClosed() from error

    def flush(self):
        try:
            self._raw.flush()
        except BrokenPipeError as error:
            raise _StdoutClosed() from error


def _stdout(stdout):
    return _StdoutWriter(stdout if stdout is not None else sys.stdout.buffer)


def _copy_stream(
    source: str, target: str, *, stdin=None, stdout=None, exclusive=False
) -> None:
    """Copy `source` to `target` in chunks; either may be `-` (stdin or
    stdout). Never holds the whole object in memory. `exclusive`: refuse an
    existing target (FileExistsError), as `cp` without `--overwrite` does."""
    if source == "-":
        reader = _stdin(stdin)
        _write_stream(reader, target, stdout=stdout, exclusive=exclusive)
        return
    with _path(source).open("rb") as reader:
        _write_stream(reader, target, stdout=stdout, exclusive=exclusive)


def _write_stream(reader, target: str, *, stdout=None, exclusive=False) -> None:
    if target == "-":
        writer = _stdout(stdout)
        shutil.copyfileobj(reader, writer, _CHUNK_SIZE)
        writer.flush()
        return
    with _path(target).open("xb" if exclusive else "wb") as writer:
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
        if args.recursive:
            raise ValueError("--recursive cannot copy from or to '-'")
        # Without --overwrite an existing target is refused, as for a file
        # source (an exclusive create: no check-then-write race).
        _copy_stream(
            args.source,
            args.target,
            stdin=stdin,
            stdout=stdout,
            exclusive=not args.overwrite,
        )
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
    except _StdoutClosed:
        # Quiet, like a POSIX tool killed by SIGPIPE. The real stdout is
        # pointed at devnull so the interpreter's exit flush cannot fail too.
        if stdout is None:
            _discard_stdout()
        return _EXIT_BROKEN_PIPE
    except KeyboardInterrupt:
        return _EXIT_INTERRUPTED
    except Exception as error:
        stream = stderr if stderr is not None else sys.stderr
        print(f"uripath: {type(error).__name__}: {error}", file=stream)
        return 1


def _discard_stdout() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
