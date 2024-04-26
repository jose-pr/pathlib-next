import pytest
import src.pathlib_next as pathlib_next


def test_scheme_and_host():
    uri = pathlib_next.Uri("http://google.com")
    assert uri.source.scheme == "http"
    assert uri.source.host == "google.com"
    assert uri.source.port == None


def test_no_scheme_with_host():
    uri = pathlib_next.Uri("//google.com/")
    assert uri.source.scheme == None
    assert uri.source.host == "google.com"
    assert uri.source.port == None
