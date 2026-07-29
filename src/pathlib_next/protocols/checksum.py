from __future__ import annotations

import typing as _ty

from .. import utils as _utils


class NativeChecksum(_ty.Protocol):
    """Composable protocol for backends that can compute a file digest
    server-side, without streaming the file's content through this
    process.

    Optional per-`Path`-subclass, like every other protocol in this
    package (`io.BinaryOpen`, `fs.Stat`, `fs.Chmod`) -- most backends will
    never implement it, and the base `Path` ABC does not require it.
    `utils.sync.PathSyncer` (and any other caller) is expected to try this
    first and fall back to a streaming checksum (`utils.checksum.md5`/
    `sha256`) on `NotImplementedError`.

    Two methods, two different jobs: `supported_checksums()` is an
    *advisory* capability query (never raises, so a caller can pick a
    shared algorithm across two paths -- e.g.
    `source.supported_checksums() & target.supported_checksums()` -- before
    calling anything expensive); `checksum()` is the *authoritative*
    per-call contract and still MUST raise `NotImplementedError` on its own
    for any algorithm it can't actually produce, even if a caller never
    consulted `supported_checksums()` first (belt-and-suspenders -- the
    checksum method's own contract, not just the advertisement, is what a
    correctness-sensitive caller can rely on).
    """

    __slots__ = ()

    def supported_checksums(self) -> "_ty.FrozenSet[str]":
        """Advisory set of `algorithm` names this path can currently
        produce a native digest for (e.g. `frozenset({"md5"})`), so a
        caller can pick a shared algorithm across two paths -- or fall back
        to streaming -- without probing via trial-and-`NotImplementedError`
        per candidate. Never raises. The default (no override, matching the
        base `Path`/`Pathname` ABC not implementing this protocol at all)
        is an empty `frozenset()` -- "no native support" -- which is also
        the correct answer for a backend whose native-hashing capability
        can vary at runtime (e.g. per-connection extension negotiation) and
        currently has none available.

        This can be a live/dynamic check (e.g. reflecting whether the
        current server connection actually advertised a given SFTP
        extension), not necessarily a hardcoded class-level constant --
        implementations should recompute it if capability can change
        within the object's lifetime, and are free to cache it if not.
        """
        return frozenset()

    @_utils.notimplemented
    def checksum(self, algorithm: str = "md5") -> str:
        """Return a hex-digest checksum of this file's content, computed by
        the backend itself (e.g. an SFTP server's `check-file@openssh.com`
        extension, a WebDAV `getetag`, ...) rather than by streaming the
        content through `open("rb")`.

        `algorithm` names a `hashlib`-style digest (at least `"md5"` must
        be accepted, matching `PathSyncer`'s current default). A backend
        that cannot produce the requested algorithm -- whether because it
        supports no native hashing at all, or only a different algorithm --
        MUST raise `NotImplementedError` rather than silently returning a
        digest under a different algorithm or a value that isn't a true
        content hash. This is a hard requirement, not a style preference:
        comparing two checksums computed under different algorithms (or
        comparing a real hash to something hash-shaped but not actually a
        content digest, e.g. S3's ETag for a multipart upload) can silently
        produce a false "in sync" verdict. Callers must never catch
        anything broader than `NotImplementedError` here.
        `supported_checksums()` not listing `algorithm` is advisory, not a
        substitute for this method enforcing its own contract.
        """
        ...
