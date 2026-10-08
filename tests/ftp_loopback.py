"""A small scriptable FTP server on 127.0.0.1 port 0, for the replies a real
server will not produce on demand: a listing with an undecodable name, an
`MLST` that answers without an entry, a refused `RNTO`, a transfer that waits.

It speaks just enough of RFC 959, 2389 and 3659 for `ftplib`, logs every
command it receives and tracks the control connections that are open. Nothing
here leaves the machine.
"""

from __future__ import annotations

import socket
import threading


class _Session:
    def __init__(self, server, conn, cid):
        self.server = server
        self.conn = conn
        self.cid = cid
        self.pasv = None
        self.rnfr = None

    def send(self, line):
        self.conn.sendall(line.encode("utf-8", "surrogateescape") + b"\r\n")

    def data(self):
        listener, self.pasv = self.pasv, None
        listener.settimeout(5)
        conn, _ = listener.accept()
        listener.close()
        return conn


class LoopbackFtp:
    """`files`: path -> bytes. `dirs`: directory paths. `listings`: directory
    path -> raw MLSD payload (bytes); `("nlst", path)` -> raw NLST payload.
    `entries`: path -> the `facts` text of its `MLST` entry. `features`: the
    lines `FEAT` lists, or None for a server that answers 502 to it.
    `overrides`: COMMAND -> fn(session, arg) -> True when it handled the
    command."""

    def __init__(
        self,
        *,
        files=None,
        dirs=("/",),
        listings=None,
        entries=None,
        features=("MLST type*;size*;modify*;", "MLSD"),
        mlsd=True,
        overrides=None,
    ):
        self.files = dict(files or {})
        self.dirs = set(dirs)
        self.listings = dict(listings or {})
        self.entries = dict(entries or {})
        self.features = None if features is None else list(features)
        self.mlsd = mlsd
        self.overrides = dict(overrides or {})
        self.log = []
        self.live = set()
        self.total = 0
        self._lock = threading.Lock()
        self._stop = False
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return f"ftp://127.0.0.1:{self.port}"

    def commands(self, clear=True):
        """The command verbs received so far, in order."""
        with self._lock:
            verbs = [command for _, command, _ in self.log]
            if clear:
                self.log.clear()
        return verbs

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _accept(self):
        cid = 0
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            cid += 1
            with self._lock:
                self.total += 1
                self.live.add(cid)
            threading.Thread(target=self._serve, args=(conn, cid), daemon=True).start()

    def _serve(self, conn, cid):
        session = _Session(self, conn, cid)
        try:
            session.send("220 loopback")
            buffer = b""
            while True:
                while b"\n" not in buffer:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buffer += chunk
                line, buffer = buffer.split(b"\n", 1)
                text = line.rstrip(b"\r").decode("utf-8", "surrogateescape")
                command, _, arg = text.partition(" ")
                command = command.upper()
                with self._lock:
                    self.log.append((cid, command, arg))
                handler = self.overrides.get(command)
                if handler is not None and handler(session, arg):
                    continue
                getattr(self, "do_" + command, self.do_unknown)(session, arg)
                if command == "QUIT":
                    return
        except OSError:
            pass
        finally:
            with self._lock:
                self.live.discard(cid)
            try:
                conn.close()
            except OSError:
                pass

    # -- commands
    def do_unknown(self, s, arg):
        s.send("502 not implemented")

    def do_USER(self, s, arg):
        s.send("331 password please")

    def do_PASS(self, s, arg):
        s.send("230 logged in")

    def do_TYPE(self, s, arg):
        s.send("200 type set")

    def do_NOOP(self, s, arg):
        s.send("200 noop")

    def do_QUIT(self, s, arg):
        s.send("221 bye")

    def do_PWD(self, s, arg):
        s.send('257 "/" is the working directory')

    def do_CWD(self, s, arg):
        s.send("250 ok" if arg in self.dirs else "550 no such directory")

    def do_FEAT(self, s, arg):
        if self.features is None:
            s.send("502 FEAT not implemented")
            return
        s.send("211-Features:")
        for feature in self.features:
            s.send(" " + feature)
        s.send("211 End")

    def do_EPSV(self, s, arg):
        s.send("500 no EPSV")

    def do_PASV(self, s, arg):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        s.pasv = listener
        port = listener.getsockname()[1]
        s.send("227 Entering Passive Mode (127,0,0,1,%d,%d)" % (port >> 8, port & 255))

    def _refuse_data(self, s, reply):
        if s.pasv:
            s.pasv.close()
            s.pasv = None
        s.send(reply)

    def _send_data(self, s, payload):
        s.send("150 here it comes")
        data = s.data()
        data.sendall(payload)
        data.close()
        s.send("226 transfer complete")

    def do_MLSD(self, s, arg):
        payload = self.listings.get(arg) if self.mlsd else None
        if not self.mlsd:
            self._refuse_data(s, "500 MLSD not understood")
        elif payload is None:
            self._refuse_data(s, "550 no such directory")
        else:
            self._send_data(s, payload)

    def do_MLST(self, s, arg):
        facts = self.entries.get(arg)
        if facts is None:
            s.send("550 no such file or directory")
            return
        s.send(f"250-Listing {arg}")
        s.send(f" {facts} {arg}")
        s.send("250 End")

    def do_NLST(self, s, arg):
        payload = self.listings.get(("nlst", arg))
        if payload is None:
            self._refuse_data(s, "550 no such directory")
        else:
            self._send_data(s, payload)

    def do_RETR(self, s, arg):
        payload = self.files.get(arg)
        if payload is None:
            self._refuse_data(s, "550 no such file")
        else:
            self._send_data(s, payload)

    def do_STOR(self, s, arg):
        s.send("150 send it")
        data = s.data()
        chunks = []
        while True:
            chunk = data.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        data.close()
        self.files[arg] = b"".join(chunks)
        s.send("226 stored")

    def do_SIZE(self, s, arg):
        if arg in self.files:
            s.send("213 %d" % len(self.files[arg]))
        else:
            s.send("550 no such file")

    def do_DELE(self, s, arg):
        if self.files.pop(arg, None) is None:
            s.send("550 no such file")
        else:
            s.send("250 deleted")

    def do_RNFR(self, s, arg):
        if arg in self.files or arg in self.dirs:
            s.rnfr = arg
            s.send("350 ready for RNTO")
        else:
            s.send("550 no such file")

    def do_RNTO(self, s, arg):
        source, s.rnfr = s.rnfr, None
        if arg in self.files or arg in self.dirs:
            s.send("550 File exists.")
        elif source in self.files:
            self.files[arg] = self.files.pop(source)
            s.send("250 renamed")
        else:
            self.dirs.discard(source)
            self.dirs.add(arg)
            s.send("250 renamed")

    def do_SITE(self, s, arg):
        s.send("200 site ok")
