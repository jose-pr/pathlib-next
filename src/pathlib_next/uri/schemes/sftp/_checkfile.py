"""Backend-native checksums through the filexfer draft's `check-file-handle`
SFTP extension (draft-ietf-secsh-filexfer-extensions-00, section 3;
implemented by e.g. ProFTPD's mod_sftp). OpenSSH implements no check-file
extension at all -- its sftp-server answers SSH_FX_OP_UNSUPPORTED, which is
cached per connection so it costs one request, not one per file.

Everything but the wire call is shared: the request's fixed fields, the
reply parser, the per-connection record of a refusal and the support probe.
A backend subclasses `CheckFileSftpBackend` and implements
`_check_file_request()` with its client library's own extended-request
primitive. Neither SSH library is imported here."""

from __future__ import annotations

import struct as _struct
import threading as _thread
import typing as _ty
import weakref as _weakref

from . import BaseSftpBackend

if _ty.TYPE_CHECKING:
    from . import SftpPath

#: The extended request's name.
EXTENSION = "check-file-handle"

#: Hash algorithm names from the draft, in preference order (md5 first:
#: PathSyncer's default, so the common case resolves in one round trip).
ALGORITHMS = ("md5", "sha1", "sha256", "sha384", "sha512")

#: Bytes of the digest each algorithm the draft names produces. A reply for
#: any other name is not a digest this module can check, so none is requested.
DIGEST_SIZES = {
    "md5": 16,
    "sha1": 20,
    "sha224": 28,
    "sha256": 32,
    "sha384": 48,
    "sha512": 64,
    "crc32": 4,
}

#: The request's fixed fields after the handle and the algorithm list:
#: uint64 start-offset, uint64 length (0: to the end of the file) and uint32
#: block-size (0: one hash over the whole range).
START_OFFSET = 0
LENGTH = 0
BLOCK_SIZE = 0

# What one connection has shown about the extension, keyed by the backend's
# client object itself (WeakKeyDictionary, not id(): a plain dict keyed by
# id() risks a stale hit if a client is GC'd and a new, unrelated object gets
# the same id(), a real risk since both backends' connection caches evict and
# replace clients over a long-running process). A reconnect (a new client
# object) starts with a clean slate. Neither library exposes the extension
# list the server sent during version negotiation, so an actual attempt
# against a real file is the only source of truth.
_SUPPORT_CACHE: "_weakref.WeakKeyDictionary" = _weakref.WeakKeyDictionary()
_SUPPORT_LOCK = _thread.Lock()


class _Support:
    """The algorithms a connection has produced a digest for, the ones the
    server answered "operation unsupported" for, and whether the probe of
    `supported_checksums()` has run. A refusal is about one algorithm: a
    server that has the extension may still refuse one (a FIPS build and
    md5), and the same request for another file may work."""

    __slots__ = ("produced", "refused", "probed")

    def __init__(self) -> None:
        self.produced: "set[str]" = set()
        self.refused: "set[str]" = set()
        self.probed = False


def _support(client) -> _Support:
    with _SUPPORT_LOCK:
        try:
            record = _SUPPORT_CACHE.get(client)
            if record is None:
                record = _SUPPORT_CACHE[client] = _Support()
            return record
        except TypeError:
            # A client that cannot be weakly referenced keeps no record.
            return _Support()


def refused(error: BaseException) -> bool:
    """Whether `error`, raised by the extension request itself, says the
    server does not implement it for this algorithm: `NotImplementedError`
    (SSH_FX_OP_UNSUPPORTED, or a reply that is not SSH_FXP_EXTENDED_REPLY).
    Any other failure -- the file vanished, permission denied, a bare
    SSH_FX_FAILURE for this one file -- says nothing about the extension."""
    return isinstance(error, NotImplementedError)


def _read_string(data: bytes) -> "tuple[bytes, bytes]":
    if len(data) < 4:
        raise NotImplementedError(f"{EXTENSION}: truncated reply")
    (size,) = _struct.unpack(">I", data[:4])
    if len(data) < 4 + size:
        raise NotImplementedError(f"{EXTENSION}: truncated reply")
    return data[4 : 4 + size], data[4 + size :]


def parse_reply(payload: bytes, algorithm: str) -> str:
    """The hex digest in a `check-file` reply's payload (the bytes after the
    request id): `[string "check-file"] string algorithm`, then the raw hash
    as the rest of the packet. Raises `NotImplementedError` for any reply
    that does not carry exactly one `algorithm` digest of the size that
    algorithm has (`DIGEST_SIZES`)."""
    expected = DIGEST_SIZES.get(algorithm)
    if expected is None:
        raise NotImplementedError(
            f"{EXTENSION}: no digest size known for {algorithm!r}"
        )
    name, digest = _read_string(payload)
    if name == b"check-file":
        # Later filexfer drafts prefix the reply with the extension name.
        name, digest = _read_string(digest)
    reply_algorithm = name.decode("utf-8", "replace")
    if reply_algorithm != algorithm:
        # A server MUST echo back one of the algorithms offered -- if it
        # names something else, don't trust the digest.
        raise NotImplementedError(
            f"{EXTENSION} returned {reply_algorithm!r}, requested {algorithm!r}"
        )
    if len(digest) != expected:
        # Any other length means a reply shape this parser does not
        # understand (a length-prefixed or truncated hash).
        raise NotImplementedError(
            f"{EXTENSION} returned a {len(digest)}-byte {algorithm} digest, "
            f"expected {expected}"
        )
    return digest.hex()


class CheckFileSftpBackend(BaseSftpBackend):
    """A `BaseSftpBackend` whose `checksum()`/`supported_checksums()` speak
    `check-file-handle`. A subclass supplies `_check_file_request()`; the
    handle is opened (and always closed) through its client's paramiko-
    shaped `open()`."""

    __slots__ = ()

    def _check_file_request(self, client, file, algorithm: str) -> bytes:
        """Send `check-file-handle` for the open `file` (as `client.open()`
        returned it) with `algorithm` as the whole algorithm list and the
        module's fixed `START_OFFSET`/`LENGTH`/`BLOCK_SIZE`. Returns the
        SSH_FXP_EXTENDED_REPLY payload after the request id; raises
        `NotImplementedError` for any other reply type. A failure status is
        raised as the library's `OSError`/`NotImplementedError` --
        `refused()` reads it."""
        raise NotImplementedError(EXTENSION)

    def checksum(self, path: "SftpPath", algorithm: str) -> str:
        """Server-side digest of `path` through `check-file-handle`. A server
        that answers "operation unsupported" for `algorithm` is not asked for
        it again on this connection (an OpenSSH server costs one request per
        algorithm, not one per file); other algorithms are still asked for.
        Other failures propagate as raised, and refuse nothing;
        `SftpPath.checksum()` translates them to `NotImplementedError`."""
        if algorithm not in DIGEST_SIZES:
            raise NotImplementedError(
                f"{EXTENSION}: {algorithm!r} is not an algorithm the draft names"
            )
        client = self.client(path.source)
        record = _support(client)
        if algorithm in record.refused:
            raise NotImplementedError(
                f"{EXTENSION}: the server does not support {algorithm}"
            )
        # The extension hashes an *open handle*, not a bare path -- open
        # read-only and unbuffered (it is never read), and always close, so
        # an attempt never leaks a handle whether it succeeds or not.
        file = client.open(path.path, "r", 0)
        try:
            try:
                payload = self._check_file_request(client, file, algorithm)
            except Exception as error:
                if refused(error):
                    record.refused.add(algorithm)
                raise
        finally:
            file.close()
        digest = parse_reply(payload, algorithm)
        record.produced.add(algorithm)
        return digest

    def supported_checksums(self, path: "SftpPath") -> "frozenset[str]":
        """Per-connection probe: try `checksum()` against `path` once and
        remember the answer for this connected client. `path` must already
        exist and be readable, or the probe's own `open()` fails for an
        unrelated reason (a missing file), which is reported as empty here
        but not remembered -- this method is advisory, never raises.

        Only the FIRST algorithm is actually attempted: a server either
        implements the extension (and then the draft's algorithm set) or it
        doesn't. Once any digest has been produced the answer is the draft's
        set less the algorithms the server refused; before that it is empty,
        and `checksum()` still asks for any algorithm not refused.
        """
        client = self.client(path.source)
        record = _support(client)
        if not record.produced and not record.probed:
            if ALGORITHMS[0] not in record.refused:
                try:
                    self.checksum(path, ALGORITHMS[0])
                except NotImplementedError:
                    pass
                except Exception:
                    # Any other failure (missing file, transport hiccup, ...)
                    # isn't evidence one way or the other about extension
                    # support -- don't remember an inconclusive probe.
                    return frozenset()
            record.probed = True
        if record.produced:
            return frozenset(a for a in ALGORITHMS if a not in record.refused)
        return frozenset()
