import pytest
import src.pathlib_next as pathlib_next
from src.pathlib_next.uri import Uri

import pathlib


def test_source():
    uri = pathlib_next.Uri("http://user:pass@google.com:80")
    assert uri.source.scheme == "http"
    assert uri.source.host == "google.com"
    assert uri.source.port == 80
    assert uri.source.parsed_userinfo() == ('user','pass')


def test_no_scheme():
    uri = pathlib_next.Uri("//user:pass@google.com:80/")
    assert uri.source.scheme == None
    assert uri.source.host == "google.com"
    assert uri.source.port == 80
    assert uri.source.parsed_userinfo() == ('user','pass')

def test_no_scheme_with_host_no_pass():
    uri = pathlib_next.Uri("//user@google.com:80/")
    assert uri.source.scheme == None
    assert uri.source.host == "google.com"
    assert uri.source.port == 80
    assert uri.source.parsed_userinfo() == ('user', '')

def test_no_scheme_no_host():
    uri = pathlib_next.Uri("//user@:80/")
    assert uri.source.scheme == None
    assert uri.source.host == ""
    assert uri.source.port == 80
    assert uri.source.parsed_userinfo() == ('user', '')

def test_no_scheme_no_netloc():
    uri = pathlib_next.Uri("//user@")
    assert uri.source.scheme == None
    assert uri.source.host == ""
    assert uri.source.port == None
    assert uri.source.parsed_userinfo() == ('user', '')

def test_path():
    uri = pathlib_next.Uri("http://google.com/root/subroot/filename.ext")
    assert uri.source.scheme == "http"
    assert uri.source.host == "google.com"
    assert uri.source.port == None
    assert uri.source.parsed_userinfo() == ('','')
    assert uri.path == '/root/subroot/filename.ext'

def test_encoded_path():
    uri = pathlib_next.Uri("http://goog%2Fe.com/root/subroot/%3Fquery/%23fragment/%2Fencoded%2Ffilename.ext")
    assert uri.source.scheme == "http"
    assert uri.source.host == "goog/e.com"
    assert uri.source.port == None
    assert uri.source.parsed_userinfo() == ('','')
    assert uri.path == '/root/subroot/?query/#fragment//encoded/filename.ext'


def test_child():
    sftp_root = Uri('sftp://root@sftpexample/')
    authkeys = sftp_root / 'root/.ssh/authorized_keys'
    uri = authkeys.as_uri()
    assert uri == 'sftp://root@sftpexample/root/.ssh/authorized_keys'

def test_truediv_pathlib():
    sftp_root = Uri('sftp://root@sftpexample/')
    authkeys = sftp_root / pathlib.PurePosixPath('root/.ssh/authorized_keys')
    uri = authkeys.as_uri()
    assert uri == 'sftp://root@sftpexample/root/.ssh/authorized_keys'
    authkeys = sftp_root / pathlib.Path('root/.ssh/authorized_keys')
    uri = authkeys.as_uri()
    assert uri == 'file:/root/.ssh/authorized_keys'
    