"""What is still alive when the test session ends: open sockets, event loops
that were not closed, child processes that have not exited and non-daemon
threads. `ResourceWarning` only fires for an object that is collected during
the run; a socket held by a module-level cache is never reported, so the
census looks at what exists instead.

Switched on by `pytest --leak-census` (a report in the terminal summary) or
`--leak-census=fail` (the session also exits non-zero). Where the run was
started with `python -X tracemalloc=N`, each item names the first frame of
`pathlib_next` or of the tests that created it; otherwise only the counts are
known.
"""

from __future__ import annotations

import asyncio
import gc
import socket
import ssl
import subprocess
import threading
import tracemalloc
from dataclasses import dataclass, field

_LIBRARY = "pathlib_next"


@dataclass
class Item:
    kind: str
    description: str
    origin: "str | None" = None
    library: bool = False


@dataclass
class Census:
    items: "list[Item]" = field(default_factory=list)

    def of(self, kind):
        return [item for item in self.items if item.kind == kind]

    def library_owned(self):
        return [item for item in self.items if item.library]

    def lines(self):
        out = []
        for item in self.items:
            where = f"  created at {item.origin}" if item.origin else ""
            out.append(f"{item.kind}: {item.description}{where}")
        return out


def _origin(obj):
    """`(frame text, owned by the library)` for the creator of `obj`, or
    `(None, False)` when tracemalloc was not running at its creation."""
    if not tracemalloc.is_tracing():
        return None, False
    trace = tracemalloc.get_object_traceback(obj)
    if trace is None:
        return None, False
    # Frames are stored oldest first: the innermost one that is ours.
    frames = [
        frame
        for frame in reversed(list(trace))
        if f"{_LIBRARY}" in frame.filename.replace("\\", "/")
        or "/tests/" in frame.filename.replace("\\", "/")
    ]
    if not frames:
        return None, False
    frame = frames[0]
    return (
        f"{frame.filename}:{frame.lineno}",
        f"/{_LIBRARY}/" in frame.filename.replace("\\", "/"),
    )


def _socket_kind(sock):
    if isinstance(sock, ssl.SSLSocket):
        return "tls socket"
    try:
        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
            return "listening socket"
    except OSError:
        pass
    return "socket"


def take_census(ignore=()) -> Census:
    """Everything alive now, after a collection, except the objects in
    `ignore`."""
    gc.collect()
    skip = {id(obj) for obj in ignore}
    census = Census()
    for obj in gc.get_objects():
        if id(obj) in skip:
            continue
        try:
            if isinstance(obj, socket.socket):
                if obj.fileno() == -1:
                    continue
                origin, library = _origin(obj)
                census.items.append(Item(_socket_kind(obj), repr(obj), origin, library))
            elif isinstance(obj, asyncio.BaseEventLoop):
                if obj.is_closed():
                    continue
                origin, library = _origin(obj)
                kind = "running loop" if obj.is_running() else "unclosed loop"
                census.items.append(Item(kind, repr(obj), origin, library))
            elif isinstance(obj, subprocess.Popen):
                if obj.poll() is None:
                    origin, library = _origin(obj)
                    census.items.append(
                        Item("running subprocess", f"pid {obj.pid}", origin, library)
                    )
        except (ReferenceError, OSError, AttributeError):
            continue
    main = threading.main_thread()
    for thread in threading.enumerate():
        if thread is not main and thread.is_alive() and not thread.daemon:
            census.items.append(Item("thread", thread.name))
    return census
