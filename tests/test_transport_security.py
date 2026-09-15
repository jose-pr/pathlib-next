"""Transport security defaults for the network schemes, asserted at the
boundary: what the server or the `requests` session actually received,
what an exception and its formatted traceback actually contain, and how
long a stalled loopback peer can hold a call. Never contacts an external
host.

- HTTP/WebDAV: URL userinfo goes out as `auth=`, never inside the request
  URL or the MOVE `Destination` header; translated errors do not chain a
  requests exception; every request has a default timeout.
- git hosting: the token comes from the password slot (or a bare
  `TOKEN@`), and the whole userinfo is redacted from str/repr/errors.
- FTP/FTPS: a 30 s default timeout; certificate + host name verification
  by default with an explicit opt-out; TLS session reuse on data
  connections; `ftps://` dispatches in a fresh process.
"""

import base64
import datetime
import http.server
import ipaddress
import os
import pathlib
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback

import pytest

requests = pytest.importorskip("requests")

from pathlib_next.uri import Source, UriPath
from pathlib_next.uri.schemes import _gitrepo
from pathlib_next.uri.schemes import http as http_scheme
from pathlib_next.uri.schemes.dav import DavPath
from pathlib_next.uri.schemes.ftp import FtpBackend, FtpPath
from pathlib_next.uri.schemes.github import GitHubPath
from pathlib_next.uri.schemes.gitlab import GitLabPath
from pathlib_next.uri.schemes.http import HttpBackend, HttpPath

SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")


def _formatted(error: BaseException) -> str:
    """Everything a log line or traceback could show for `error`."""
    return "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ) + "\n".join((str(error), repr(error)))


# --- loopback servers -------------------------------------------------------


@pytest.fixture
def recording_http_server():
    """`/<code>` answers that status, `/redirect` 302s to `/final`, `/final`
    and `/ok` answer 200 with a body. Yields (base_url, requests) where each
    request is recorded as (method, path, headers)."""
    seen = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self):
            seen.append((self.command, self.path, dict(self.headers)))
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            path = self.path.lstrip("/")
            if path == "redirect":
                self.send_response(302)
                self.send_header("Location", "/final")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path in ("final", "ok"):
                body = b"ok"
                self.send_response(200)
            else:
                body = b""
                try:
                    self.send_response(int(path))
                except ValueError:
                    self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = do_PUT = do_DELETE = do_MOVE = do_PROPFIND = _handle

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def stalled_server():
    """Accepts TCP connections and never sends a byte. Yields the port."""
    listener = socket.create_server(("127.0.0.1", 0))
    held = []
    stop = threading.Event()

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except (socket.timeout, OSError):
                continue
            held.append(conn)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join(timeout=5)
        for conn in held:
            conn.close()
        listener.close()


class _RecordingSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return super().request(method, url, **kwargs)


# --- HTTP / WebDAV: credentials ---------------------------------------------


def test_http_sends_userinfo_as_auth_not_in_url(recording_http_server):
    host, seen = recording_http_server
    session = _RecordingSession()
    p = UriPath(f"http://alice:s3cr3t@{host}/ok").with_session(session)
    assert p.read_bytes() == b"ok"

    method, url, kwargs = session.calls[-1]
    assert url == f"http://{host}/ok"
    assert kwargs["auth"] == ("alice", "s3cr3t")
    expected = "Basic " + base64.b64encode(b"alice:s3cr3t").decode()
    assert seen[-1][2]["Authorization"] == expected


def test_http_percent_encoded_userinfo_is_decoded_for_auth(recording_http_server):
    host, seen = recording_http_server
    session = _RecordingSession()
    p = HttpPath(f"http://al%40ice:p%3Aw@{host}/ok").with_session(session)
    p.read_bytes()
    assert session.calls[-1][1] == f"http://{host}/ok"
    assert session.calls[-1][2]["auth"] == ("al@ice", "p:w")


def test_http_explicit_auth_wins_over_userinfo():
    session = _FakeSession()
    p = HttpPath(
        "http://alice:s3cr3t@h/x", backend=HttpBackend(session, {"auth": ("bob", "b")})
    )
    p.backend.request("GET", p)
    method, url, kwargs = session.calls[-1]
    assert url == "http://h/x"
    assert kwargs["auth"] == ("bob", "b")


def test_http_redirect_response_urls_carry_no_credentials(recording_http_server):
    host, _seen = recording_http_server
    p = UriPath(f"http://alice:s3cr3t@{host}/redirect")
    resp = p.backend.request("GET", p)
    urls = [r.url for r in resp.history] + [resp.url]
    assert urls == [f"http://{host}/redirect", f"http://{host}/final"]
    assert resp.request.headers["Authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    "path, exc",
    [("500", OSError), ("404", FileNotFoundError), ("403", PermissionError)],
)
def test_http_error_text_and_traceback_carry_no_password(
    recording_http_server, path, exc
):
    host, _seen = recording_http_server
    p = UriPath(f"http://alice:s3cr3t@{host}/{path}")
    with pytest.raises(exc) as excinfo:
        p.read_bytes()
    text = _formatted(excinfo.value)
    assert "s3cr3t" not in text
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_http_error_message_keeps_status():
    session = _FakeSession()
    session.responses[("GET", "http://h/x")] = _FakeResponse(502, reason="Bad Gateway")
    p = HttpPath("http://alice:s3cr3t@h/x", backend=HttpBackend(session, {}))
    with pytest.raises(OSError) as excinfo:
        p.read_bytes()
    assert "HTTP Error 502 Bad Gateway" in str(excinfo.value)


def test_dav_error_and_move_destination_carry_no_password(recording_http_server):
    host, seen = recording_http_server
    p = DavPath(f"dav://alice:s3cr3t@{host}/501")
    with pytest.raises(OSError) as excinfo:
        p.rename("b.txt")
    assert "s3cr3t" not in _formatted(excinfo.value)
    method, path, headers = seen[-1]
    assert method == "MOVE"
    assert headers["Destination"] == f"http://{host}/b.txt"
    assert headers["Authorization"].startswith("Basic ")


def test_dav_requests_strip_userinfo():
    session = _FakeSession()
    session.responses[("MOVE", "http://host/docs/a.txt")] = _FakeResponse(201)
    p = DavPath("dav://alice:pw@host/docs/a.txt", backend=HttpBackend(session, {}))
    p.rename("b.txt")
    method, url, kwargs = session.calls[-1]
    assert url == "http://host/docs/a.txt"
    assert kwargs["auth"] == ("alice", "pw")
    assert kwargs["headers"]["Destination"] == "http://host/docs/b.txt"


# --- HTTP: timeouts -----------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, reason="", content=b""):
        self.status_code = status_code
        self.reason = reason
        self.content = content
        self.headers = {}
        self.is_redirect = False
        self.url = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def close(self):
        pass


class _FakeSession:
    auth = None

    def __init__(self):
        self.calls = []
        self.responses = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.get((method, url)) or _FakeResponse(404)


def test_http_default_timeout_applied():
    session = _FakeSession()
    HttpBackend(session, {}).request("GET", "http://h/x")
    assert session.calls[-1][2]["timeout"] == (10, 60)
    assert http_scheme.DEFAULT_TIMEOUT == (10, 60)


@pytest.mark.parametrize("timeout", [5, None])
def test_http_caller_timeout_wins(timeout):
    session = _FakeSession()
    HttpBackend(session, {"timeout": timeout}).request("GET", "http://h/x")
    assert session.calls[-1][2]["timeout"] == timeout
    HttpBackend(session, {}).request("GET", "http://h/x", timeout=timeout)
    assert session.calls[-1][2]["timeout"] == timeout


def test_http_stalled_server_times_out_by_default(stalled_server, monkeypatch):
    # The default path (no caller timeout), shortened for the test.
    monkeypatch.setattr(http_scheme, "DEFAULT_TIMEOUT", (2, 0.5))
    p = UriPath(f"http://127.0.0.1:{stalled_server}/x")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        p.stat()
    assert time.monotonic() - started < 10


# --- git hosting: token slot, redaction, timeout ----------------------------


@pytest.mark.parametrize(
    "userinfo, token",
    [
        ("ghp_BARE123", "ghp_BARE123"),
        ("x-access-token:ghp_REAL456", "ghp_REAL456"),
        ("oauth2:glpat-REAL789", "glpat-REAL789"),
    ],
)
@pytest.mark.parametrize("cls", [GitHubPath, GitLabPath])
def test_git_token_slot_and_redaction(recording_http_server, cls, userinfo, token):
    host, seen = recording_http_server
    scheme = "github" if cls is GitHubPath else "gitlab"
    p = cls(f"{scheme}://{userinfo}@example.test/acme/widgets/a.txt")
    assert p.backend.token == token
    user = userinfo.partition(":")[0]

    child = p.parent / "b.txt"
    shown = [str(p), repr(p), p.as_uri(sanitize=True), str(child), repr(child)]
    for text in shown:
        assert token not in text and user not in text
        assert "@" not in text
    # the full round trip is still available explicitly
    assert p.as_uri() == f"{scheme}://{userinfo}@example.test/acme/widgets/a.txt"

    p.backend.api_base = f"http://{host}"
    with pytest.raises(FileNotFoundError) as excinfo:
        p.read_bytes()
    assert seen[-1][2]["Authorization"] == f"Bearer {token}"
    text = _formatted(excinfo.value)
    assert token not in text and user not in text
    assert excinfo.value.__cause__ is None


def test_git_dispatch_error_redacts_token():
    # Built outside the raising line: a traceback prints that source line.
    url = "git://" + "ghp_SECRET123" + "@ghe.internal/acme/widgets/README.md"
    with pytest.raises(ValueError) as excinfo:
        UriPath(url)
    assert "ghp_SECRET123" not in _formatted(excinfo.value)
    assert "ghe.internal/acme/widgets/README.md" in str(excinfo.value)


def test_git_default_timeout_applied():
    session = _FakeSession()
    _gitrepo.RepoBackend(session=session).request("GET", "http://h/x")
    assert session.calls[-1][2]["timeout"] == (10, 60)
    _gitrepo.RepoBackend(session=session, timeout=None).request("GET", "http://h/x")
    assert session.calls[-1][2]["timeout"] is None


def test_git_stalled_server_times_out_by_default(stalled_server, monkeypatch):
    monkeypatch.setattr(_gitrepo, "DEFAULT_TIMEOUT", (2, 0.5))
    backend = _gitrepo.RepoBackend(api_base=f"http://127.0.0.1:{stalled_server}")
    p = GitHubPath("github://github.com/acme/widgets/a.txt", backend=backend)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        p.stat()
    assert time.monotonic() - started < 10


# --- FTP: timeout -----------------------------------------------------------


def test_ftp_default_timeout_reaches_ftplib(monkeypatch):
    import ftplib

    received = []

    class _FakeClient:
        def __init__(self, timeout=None):
            received.append(timeout)

        def connect(self, host, port):
            pass

        def login(self, user, password):
            pass

        def set_pasv(self, value):
            pass

    monkeypatch.setattr(ftplib, "FTP", _FakeClient)
    FtpBackend().client(Source("ftp", None, "host", None), tls=False)
    assert received == [30.0]
    assert FtpPath("ftp://host/x").backend.timeout == 30.0


def test_ftp_stalled_server_times_out(stalled_server):
    p = FtpPath(f"ftp://127.0.0.1:{stalled_server}/x", backend=FtpBackend(timeout=0.5))
    started = time.monotonic()
    with pytest.raises((socket.timeout, TimeoutError)):
        p.stat()
    assert time.monotonic() - started < 10


# --- FTPS: verification, opt-out, session reuse -----------------------------


def _make_self_signed_cert(directory):
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certfile = pathlib.Path(directory, "cert.pem")
    keyfile = pathlib.Path(directory, "key.pem")
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(certfile), str(keyfile)


class _FtpsServer:
    """Minimal explicit-FTPS server on stdlib `ssl` (pyftpdlib's TLS handler
    needs pyOpenSSL). Enforces TLS session reuse on data connections the
    way vsftpd's default `require_ssl_reuse=YES` does: a data connection
    that does not resume the control session gets `522`."""

    def __init__(self, certfile, keyfile, files):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certfile, keyfile)
        self.files = files
        self.commands = []
        self.data_session_reused = []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.listener.close()

    def _serve(self):
        while True:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(sock,), daemon=True).start()

    @staticmethod
    def _readline(sock):
        line = b""
        while not line.endswith(b"\r\n"):
            chunk = sock.recv(1)
            if not chunk:
                return None
            line += chunk
        return line[:-2].decode()

    def _handle(self, sock):
        try:
            self._session(sock)
        except (OSError, ssl.SSLError):
            pass
        finally:
            sock.close()

    def _session(self, sock):
        def send(text):
            sock.sendall(text.encode() + b"\r\n")

        send("220 ready")
        prot_p = False
        data_listener = None
        while True:
            line = self._readline(sock)
            if line is None:
                return
            self.commands.append(line)
            verb, _, arg = line.partition(" ")
            verb = verb.upper()
            if verb == "AUTH":
                send("234 proceed")
                sock = self.context.wrap_socket(sock, server_side=True)
            elif verb == "USER":
                send("331 password")
            elif verb == "PASS":
                send("230 logged in")
            elif verb in ("PBSZ", "TYPE", "NOOP"):
                send("200 ok")
            elif verb == "PROT":
                prot_p = arg.upper() == "P"
                send("200 ok")
            elif verb == "PASV":
                data_listener = socket.create_server(("127.0.0.1", 0))
                port = data_listener.getsockname()[1]
                send(f"227 Entering Passive Mode (127,0,0,1,{port >> 8},{port & 255})")
            elif verb == "RETR":
                if arg not in self.files:
                    send("550 not found")
                    continue
                send("150 opening data connection")
                conn, _ = data_listener.accept()
                data_listener.close()
                if prot_p:
                    conn = self.context.wrap_socket(conn, server_side=True)
                    reused = conn.session_reused
                    self.data_session_reused.append(reused)
                    if not reused:
                        conn.close()
                        send("522 SSL connection failed: session reuse required")
                        continue
                    conn.sendall(self.files[arg])
                    conn = conn.unwrap()
                else:
                    conn.sendall(self.files[arg])
                conn.close()
                send("226 transfer complete")
            elif verb == "QUIT":
                send("221 bye")
                return
            else:
                send("502 not implemented")


@pytest.fixture
def ftps_server(tmp_path):
    certfile, keyfile = _make_self_signed_cert(tmp_path)
    server = _FtpsServer(certfile, keyfile, {"/a.txt": b"hello"})
    try:
        yield server, certfile
    finally:
        server.close()


def test_ftps_self_signed_rejected_by_default_before_password(ftps_server):
    server, _certfile = ftps_server
    # The default route: dispatch + the backend `_initbackend()` builds.
    p = UriPath(f"ftps://alice:s3cret@127.0.0.1:{server.port}/a.txt")
    assert isinstance(p, FtpPath)
    with pytest.raises(ssl.SSLCertVerificationError):
        p.read_bytes()
    assert server.commands == ["AUTH TLS"]
    assert not any(cmd.startswith(("USER", "PASS")) for cmd in server.commands)


def test_ftps_verify_false_opt_out_accepts(ftps_server):
    server, _certfile = ftps_server
    p = FtpPath(
        f"ftps://alice:s3cret@127.0.0.1:{server.port}/a.txt",
        backend=FtpBackend(timeout=5, verify=False),
    )
    assert p.read_bytes() == b"hello"
    assert "PASS s3cret" in server.commands
    assert "PROT P" in server.commands


def test_ftps_custom_ssl_context_verifies_against_its_ca(ftps_server):
    server, certfile = ftps_server
    context = ssl.create_default_context(cafile=certfile)
    p = FtpPath(
        f"ftps://alice:s3cret@127.0.0.1:{server.port}/a.txt",
        backend=FtpBackend(timeout=5, ssl_context=context),
    )
    assert p.read_bytes() == b"hello"
    assert server.data_session_reused == [True]


def test_ftps_default_context_verifies():
    context = FtpBackend()._tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_ftps_data_connection_reuses_tls_session(ftps_server):
    server, _certfile = ftps_server
    p = FtpPath(
        f"ftps://alice:s3cret@127.0.0.1:{server.port}/a.txt",
        backend=FtpBackend(timeout=5, verify=False),
    )
    assert p.read_bytes() == b"hello"
    assert p.read_bytes() == b"hello"
    assert server.data_session_reused == [True, True]


# --- ftps dispatch in a fresh process ---------------------------------------


@pytest.mark.parametrize(
    "disable", ["", "_load_builtin_scheme", "_load_entry_point"], ids=str
)
def test_ftps_dispatches_in_fresh_process(disable):
    # `disable` knocks out one registry so the other is shown to carry ftps
    # on its own ("" = the normal lookup).
    code = "\n".join(
        [
            "from unittest import mock",
            "from pathlib_next.uri import UriPath",
            f"name = {disable!r}",
            "ctx = mock.patch.object(UriPath, name, return_value=False) if name "
            "else mock.MagicMock()",
            "with ctx:",
            "    p = UriPath('ftps://u:p@127.0.0.1:1/x')",
            "print(type(p).__module__ + '.' + type(p).__name__)",
        ]
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "pathlib_next.uri.schemes.ftp.FtpPath"
