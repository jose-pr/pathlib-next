"""Unit-only FTP tests: mock BaseFtpBackend, no real server. Covers MLSD
listing/stat, the NLST/SIZE fallback path, upload-on-close writes, and
rename/chmod/mkdir/unlink error translation.
"""
import ftplib

import pytest

from pathlib_next.uri import Source, Uri
from pathlib_next.uri.schemes.ftp import BaseFtpBackend, FtpPath


class _FakeFtpClient:
    def __init__(self):
        self.mlsd_data = {}
        self.mlsd_unsupported = False
        self.nlst_data = {}
        self.size_data = {}
        self.retrieved = {}
        self.stored = {}
        self.append_calls = []
        self.delete_calls = []
        self.mkd_calls = []
        self.rmd_calls = []
        self.rename_calls = []
        self.voidcmd_calls = []

    def voidcmd(self, cmd):
        self.voidcmd_calls.append(cmd)
        return "200"

    def mlsd(self, path):
        if self.mlsd_unsupported:
            raise ftplib.error_perm("500 MLSD not understood")
        return iter(self.mlsd_data.get(path, []))

    def nlst(self, path):
        return self.nlst_data.get(path, [])

    def size(self, path):
        if path not in self.size_data:
            raise ftplib.error_perm("550 No such file")
        return self.size_data[path]

    def retrbinary(self, cmd, callback):
        _, path = cmd.split(" ", 1)
        if path not in self.retrieved:
            raise ftplib.error_perm("550 No such file")
        callback(self.retrieved[path])

    def storbinary(self, cmd, fileobj):
        verb, path = cmd.split(" ", 1)
        data = fileobj.read()
        if verb == "APPE":
            self.stored[path] = self.stored.get(path, b"") + data
            self.append_calls.append(path)
        else:
            self.stored[path] = data

    def mkd(self, path):
        self.mkd_calls.append(path)

    def delete(self, path):
        self.delete_calls.append(path)

    def rmd(self, path):
        self.rmd_calls.append(path)

    def rename(self, path, target):
        self.rename_calls.append((path, target))


class _FakeBackend(BaseFtpBackend):
    def __init__(self):
        self.client_calls = 0
        self._client = _FakeFtpClient()

    def client(self, source, tls):
        self.client_calls += 1
        return self._client


def _ftp(path, backend=None):
    return FtpPath(path, backend=backend or _FakeBackend())


# --- connection caching ---


def test_ftpclient_cached_across_accesses():
    backend = _FakeBackend()
    p = _ftp("ftp://host/a", backend=backend)
    c1 = p._ftpclient
    c2 = p._ftpclient
    assert c1 is c2
    assert backend.client_calls == 1


def test_ftpclient_recreated_when_noop_fails():
    backend = _FakeBackend()
    p = _ftp("ftp://host/a", backend=backend)
    c1 = p._ftpclient

    def failing_noop(cmd):
        raise OSError("broken pipe")

    c1.voidcmd = failing_noop
    backend._client = _FakeFtpClient()  # what a fresh connect would return
    c2 = p._ftpclient
    assert c2 is not c1
    assert backend.client_calls == 2


def test_ftps_scheme_requests_tls():
    backend = _FakeBackend()
    calls = []
    backend.client = lambda source, tls: (calls.append(tls), _FakeFtpClient())[1]
    p = FtpPath("ftps://host/a", backend=backend)
    p._ftpclient
    assert calls == [True]


def test_ftp_scheme_no_tls():
    backend = _FakeBackend()
    calls = []
    backend.client = lambda source, tls: (calls.append(tls), _FakeFtpClient())[1]
    p = FtpPath("ftp://host/a", backend=backend)
    p._ftpclient
    assert calls == [False]


# --- listdir: MLSD, falling back to NLST ---


def test_listdir_via_mlsd():
    backend = _FakeBackend()
    backend._client.mlsd_data["/dir"] = [
        (".", {"type": "cdir"}),
        ("..", {"type": "pdir"}),
        ("a.txt", {"type": "file", "size": "3"}),
        ("sub", {"type": "dir"}),
    ]
    p = _ftp("ftp://host/dir", backend=backend)
    assert sorted(p._listdir()) == ["a.txt", "sub"]


def test_listdir_falls_back_to_nlst_when_mlsd_unsupported():
    backend = _FakeBackend()
    backend._client.mlsd_unsupported = True
    backend._client.nlst_data["/dir"] = ["/dir/a.txt", "/dir/sub"]
    p = _ftp("ftp://host/dir", backend=backend)
    assert sorted(p._listdir()) == ["a.txt", "sub"]


# --- stat: MLSD, falling back to SIZE ---


def test_stat_file_via_mlsd():
    backend = _FakeBackend()
    backend._client.mlsd_data["/dir"] = [
        ("a.txt", {"type": "file", "size": "5", "modify": "20260101120000"}),
    ]
    p = _ftp("ftp://host/dir/a.txt", backend=backend)
    st = p.stat()
    assert st.st_size == 5
    assert not st.is_dir()
    assert st.st_mtime > 0


def test_stat_dir_via_mlsd():
    backend = _FakeBackend()
    backend._client.mlsd_data["/"] = [("dir", {"type": "dir"})]
    p = _ftp("ftp://host/dir", backend=backend)
    assert p.stat().is_dir()


def test_stat_fallback_size_when_mlsd_unsupported():
    backend = _FakeBackend()
    backend._client.mlsd_unsupported = True
    backend._client.size_data["/a.txt"] = 7
    p = _ftp("ftp://host/a.txt", backend=backend)
    st = p.stat()
    assert st.st_size == 7
    assert not st.is_dir()


def test_stat_missing_raises_file_not_found():
    backend = _FakeBackend()
    backend._client.mlsd_unsupported = True
    p = _ftp("ftp://host/missing.txt", backend=backend)
    with pytest.raises(FileNotFoundError):
        p.stat()


# --- open: RETR/STOR/APPE ---


def test_open_read_downloads_via_retrbinary():
    backend = _FakeBackend()
    backend._client.retrieved["/a.txt"] = b"hello"
    p = _ftp("ftp://host/a.txt", backend=backend)
    assert p.read_bytes() == b"hello"


def test_open_read_missing_raises_file_not_found():
    backend = _FakeBackend()
    p = _ftp("ftp://host/missing.txt", backend=backend)
    with pytest.raises(FileNotFoundError):
        p.read_bytes()


def test_open_write_uploads_via_storbinary_on_close():
    backend = _FakeBackend()
    p = _ftp("ftp://host/a.txt", backend=backend)
    p.write_bytes(b"hello")
    assert backend._client.stored["/a.txt"] == b"hello"


def test_open_append_uses_appe():
    backend = _FakeBackend()
    p = _ftp("ftp://host/a.txt", backend=backend)
    with p.open("ab") as f:
        f.write(b"hello")
    assert backend._client.append_calls == ["/a.txt"]
    assert backend._client.stored["/a.txt"] == b"hello"


# --- mkdir/unlink error translation ---


def test_mkdir_creates_via_mkd():
    backend = _FakeBackend()
    p = _ftp("ftp://host/newdir", backend=backend)
    p.mkdir()
    assert backend._client.mkd_calls == ["/newdir"]


def test_mkdir_existing_raises_file_exists_error():
    backend = _FakeBackend()
    backend._client.mlsd_data["/"] = [("newdir", {"type": "dir"})]

    def mkd(path):
        raise ftplib.error_perm("550 Directory already exists")

    backend._client.mkd = mkd
    p = _ftp("ftp://host/newdir", backend=backend)
    with pytest.raises(FileExistsError):
        p.mkdir()


def test_unlink_deletes_via_delete():
    backend = _FakeBackend()
    p = _ftp("ftp://host/a.txt", backend=backend)
    p.unlink()
    assert backend._client.delete_calls == ["/a.txt"]


def test_unlink_missing_ok_swallows_error():
    backend = _FakeBackend()

    def delete(path):
        raise ftplib.error_perm("550 No such file")

    backend._client.delete = delete
    p = _ftp("ftp://host/missing.txt", backend=backend)
    p.unlink(missing_ok=True)


def test_unlink_missing_without_missing_ok_raises():
    backend = _FakeBackend()

    def delete(path):
        raise ftplib.error_perm("550 No such file")

    backend._client.delete = delete
    p = _ftp("ftp://host/missing.txt", backend=backend)
    with pytest.raises(FileNotFoundError):
        p.unlink()


# --- rename: sibling-relative str target (mirrors sftp.py) ---


def test_rename_uses_target_path():
    backend = _FakeBackend()
    p = _ftp("ftp://host/a.txt", backend=backend)
    target = Uri("ftp://host/b.txt")
    p.rename(target)
    assert backend._client.rename_calls == [("/a.txt", "/b.txt")]


def test_rename_accepts_str_target():
    backend = _FakeBackend()
    p = _ftp("ftp://host/a.txt", backend=backend)
    p.rename("b.txt")
    assert backend._client.rename_calls == [("/a.txt", "/b.txt")]


# --- chmod: SITE CHMOD ---


def test_chmod_sends_site_chmod():
    backend = _FakeBackend()
    p = _ftp("ftp://host/a.txt", backend=backend)
    p.chmod(0o644)
    assert "SITE CHMOD 644 /a.txt" in backend._client.voidcmd_calls


def test_chmod_follow_symlinks_false_raises_notimplemented():
    backend = _FakeBackend()
    p = _ftp("ftp://host/a.txt", backend=backend)
    with pytest.raises(NotImplementedError):
        p.chmod(0o644, follow_symlinks=False)
