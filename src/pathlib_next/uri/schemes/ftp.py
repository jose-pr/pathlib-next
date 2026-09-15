from __future__ import annotations

import datetime as _dt
import ftplib as _ftplib
import io as _io
import ssl as _ssl
import threading as _thread
import typing as _ty

import netimps as _netimps

from ... import utils as _utils
from ...utils.stat import FileStat
from .. import Source, Uri, UriPath


class BaseFtpBackend(object):
    """Protocol for obtaining a connected+logged-in `ftplib.FTP` (or
    `FTP_TLS`) for a `Source`. Subclass this to plug in custom connection
    handling (e.g. tests mock it directly, no real server);
    `FtpBackend` is the real implementation."""

    __slots__ = ()

    @_utils.notimplemented
    def client(self, source: Source, tls: bool) -> "_ftplib.FTP": ...


DEFAULT_TIMEOUT = 30.0
"""Socket timeout, in seconds, `FtpBackend` applies to connect, replies and
transfers when the caller does not pass one."""


class _SessionReuseFTP_TLS(_ftplib.FTP_TLS):
    """`FTP_TLS` whose data connections resume the control connection's TLS
    session. Stdlib `ntransfercmd` wraps the data socket without
    `session=`, and servers such as vsftpd (`require_ssl_reuse=YES`, the
    default) and FileZilla Server reject such a data connection with
    `522`. With TLS 1.3 the session ticket arrives after the handshake; by
    the first transfer the login replies have been read, so it is there."""

    def ntransfercmd(self, cmd, rest=None):
        # Skip FTP_TLS.ntransfercmd (it wraps without `session=`).
        conn, size = super(_ftplib.FTP_TLS, self).ntransfercmd(cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=getattr(self.sock, "session", None),
            )
        return conn, size


class FtpBackend(BaseFtpBackend):
    """Connects via stdlib `ftplib.FTP` (`ftp:`) or `ftplib.FTP_TLS`
    (`ftps:`, with `PROT P` for an encrypted data channel too).

    `timeout` (seconds, default `DEFAULT_TIMEOUT` = 30) bounds connect,
    every reply and every transfer read; `None` blocks forever.

    `ftps:` verifies the server certificate and host name by default
    (`ssl.create_default_context()`), before `USER`/`PASS` are sent. To
    trust a private CA or a self-signed certificate, pass your own
    `ssl_context` (e.g. `ssl.create_default_context(cafile=...)`). To turn
    verification off entirely -- accepting any certificate, so anyone on
    the network path can read the password -- pass `verify=False`.
    `ssl_context` wins over `verify` when both are given. Data connections
    reuse the control connection's TLS session."""

    __slots__ = ("timeout", "ssl_context", "verify")

    def __init__(
        self,
        timeout: "float | None" = DEFAULT_TIMEOUT,
        ssl_context: "_ssl.SSLContext | None" = None,
        verify: bool = True,
    ) -> None:
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.verify = verify

    def _tls_context(self) -> "_ssl.SSLContext":
        if self.ssl_context is not None:
            return self.ssl_context
        context = _ssl.create_default_context()
        if not self.verify:
            context.check_hostname = False
            context.verify_mode = _ssl.CERT_NONE
        return context

    def client(self, source: Source, tls: bool):
        if tls:
            client = _SessionReuseFTP_TLS(
                context=self._tls_context(), timeout=self.timeout
            )
        else:
            client = _ftplib.FTP(timeout=self.timeout)
        try:
            client.connect(
                str(source.host), source.port or _netimps.get_default_port("ftp")
            )
            user, password = source.parsed_userinfo()
            client.login(user or "anonymous", password or "")
            if tls:
                client.prot_p()
            client.set_pasv(True)
        except BaseException:
            # A rejected certificate or a timed-out greeting must not leave
            # the half-open control socket behind.
            close = getattr(client, "close", None)
            if close is not None:
                close()
            raise
        return client


def _create_ftpclient(
    backend: BaseFtpBackend, source: Source, tls: bool, thread_id: int
):
    return backend.client(source, tls)


_CACHED_CLIENTS = _utils.LRU(_create_ftpclient, maxsize=128)


def _parse_mlsd_time(value: str) -> int:
    # MLSD "modify" fact: YYYYMMDDHHMMSS[.sss], always UTC (RFC 3659).
    try:
        return int(_dt.datetime.strptime(value[:14], "%Y%m%d%H%M%S").timestamp())
    except ValueError:
        return 0


class _FtpWriteStream(_io.BytesIO):
    """Buffers the whole write in memory, uploads on close() via
    STOR/APPE. Simple and works with any ftplib client, at the cost of
    holding the full file content in memory for the duration of the write."""

    def __init__(self, client: "_ftplib.FTP", path: str, append: bool = False):
        super().__init__()
        self._client = client
        self._path = path
        self._cmd = "APPE" if append else "STOR"

    def close(self):
        if not self.closed:
            self.seek(0)
            self._client.storbinary(f"{self._cmd} {self._path}", self)
        super().close()


class FtpPath(UriPath):
    """`ftp:`/`ftps:` scheme: full read/write access via stdlib `ftplib`,
    with a thread-keyed LRU connection cache (`_CACHED_CLIENTS`, mirroring
    `sftp.py`). Directory listing and stat prefer MLSD (RFC 3659 -- gives
    type/size/modify facts in one round trip); servers that don't support it
    fall back to NLST for listing (names only) and SIZE for file stat
    (no portable "not found vs. is a directory" distinction in that path)."""

    __SCHEMES = ("ftp", "ftps")
    __slots__ = ()

    if _ty.TYPE_CHECKING:
        backend: BaseFtpBackend

    def _initbackend(self):
        return FtpBackend()

    @property
    def _tls(self):
        return self.source.scheme == "ftps"

    @property
    def _ftpclient(self) -> "_ftplib.FTP":
        thread_id = _thread.get_ident()
        client = _CACHED_CLIENTS(self.backend, self.source, self._tls, thread_id)
        try:
            client.voidcmd("NOOP")
        except (OSError, EOFError, _ftplib.error_temp, _ftplib.error_proto):
            client = _CACHED_CLIENTS.invalidate(
                self.backend, self.source, self._tls, thread_id
            )
        return client

    def _mlsd_entry(self):
        """This entry's MLSD facts from its parent's listing, or None if
        not found or the server doesn't support MLSD."""
        parent = self.path.rsplit("/", 1)[0] or "/"
        try:
            for name, facts in self._ftpclient.mlsd(parent):
                if name == self.name:
                    return facts
        except _ftplib.error_perm:
            return None
        return None

    def _facts_to_filestat(self, facts: dict) -> FileStat:
        kind = facts.get("type", "file")
        size = int(facts.get("size", 0) or 0)
        modify = facts.get("modify")
        mtime = _parse_mlsd_time(modify) if modify else 0
        return FileStat(
            st_size=size, st_mtime=mtime, is_dir=kind in ("dir", "cdir", "pdir")
        )

    def _scandir(self):
        # MLSD's facts already carry type/size/modify for every child in
        # one round trip -- reuse them instead of `iterdir()` + a separate
        # stat per child. Falls back to NLST (names only, no metadata) on
        # servers that don't support MLSD.
        try:
            for name, facts in self._ftpclient.mlsd(self.path):
                if name not in (".", ".."):
                    yield name, self._facts_to_filestat(facts)
        except _ftplib.error_perm:
            for name in self._ftpclient.nlst(self.path):
                base = name.rsplit("/", 1)[-1]
                if base not in (".", ".."):
                    yield base, None

    def _listdir(self):
        for name, _stat in self._scandir():
            yield name

    def stat(self, *, follow_symlinks=True):
        hint = self._pop_stat_hint()
        if hint is not None:
            return hint
        # Special case: the FTP root '/' has no parent to MLSD and SIZE won't
        # work on a directory.  Confirm it exists via CWD.
        if self.path in ("/", ""):
            try:
                self._ftpclient.cwd("/")
                return FileStat(is_dir=True)
            except _ftplib.error_perm as error:
                raise FileNotFoundError(self) from error
        facts = self._mlsd_entry()
        if facts is not None:
            return self._facts_to_filestat(facts)
        # MLSD unsupported (or entry not found via it) -- SIZE only works
        # for files, so this can't tell "missing" apart from "is a
        # directory"; either way there's no file to report a size for.
        try:
            size = self._ftpclient.size(self.path)
        except _ftplib.error_perm as error:
            raise FileNotFoundError(self) from error
        if size is None:
            raise FileNotFoundError(self)
        return FileStat(st_size=size, is_dir=False)

    def _open(self, mode="r", buffering=-1):
        if "r" in mode:
            buf = _io.BytesIO()
            try:
                self._ftpclient.retrbinary(f"RETR {self.path}", buf.write)
            except _ftplib.error_perm as error:
                raise FileNotFoundError(self) from error
            buf.seek(0)
            return buf
        if mode not in ("w", "x", "a"):
            raise NotImplementedError(f"open(mode={mode!r})")
        if mode == "x" and self.exists():
            raise FileExistsError(self)
        return _FtpWriteStream(self._ftpclient, self.path, append=(mode == "a"))

    def _mkdir(self, mode):
        try:
            self._ftpclient.mkd(self.path)
        except _ftplib.error_perm as error:
            if self.exists():
                raise FileExistsError(self) from error
            raise FileNotFoundError(self) from error

    def unlink(self, missing_ok=False):
        try:
            self._ftpclient.delete(self.path)
        except _ftplib.error_perm as error:
            if missing_ok and not self.exists():
                return
            raise FileNotFoundError(self) from error

    def rmdir(self):
        try:
            self._ftpclient.rmd(self.path)
        except _ftplib.error_perm as error:
            # 550 = directory not empty or does not exist
            code = str(error)[:3]
            if code == "550":
                if self.exists():
                    raise OSError(f"Directory not empty: {self}") from error
                raise FileNotFoundError(self) from error
            raise

    def rename(self, target: "FtpPath | Uri | str"):
        # A plain str target is a sibling rename (relative to self's
        # parent), matching sftp.py's rename() semantics.
        target = self._rename_target(target)
        self._ftpclient.rename(self.path, target.path)

    def chmod(self, mode: int | str, *, follow_symlinks: bool = True):
        # SITE CHMOD is a non-standard FTP extension; pyftpdlib and many real
        # servers do not support it.  Convert any server rejection (error_perm)
        # to NotImplementedError so that path.py's copy() silently skips it.
        if not follow_symlinks:
            raise NotImplementedError("chmod(follow_symlinks=False)")
        mode = _utils.as_mode(mode)
        try:
            self._ftpclient.voidcmd(f"SITE CHMOD {mode:o} {self.path}")
        except _ftplib.error_perm:
            raise NotImplementedError("SITE CHMOD not supported by this server")
