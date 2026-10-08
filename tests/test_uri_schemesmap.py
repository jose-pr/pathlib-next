"""`schemesmap=` is the allow-list of classes a path dispatches to: the path
keeps it, and every class chosen after construction -- a join with a URI, a
new source, a `copy()`/`move()` string destination, an archive's outer URI --
is chosen from it. It restricts dispatch only; it is not a sandbox."""

from __future__ import annotations

import zipfile

import pytest

pytest.importorskip("uritools")

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes import DataUri, FileUri, FtpPath, ZipUri
from pathlib_next.uri.source import Source

ALLOW = {"data": DataUri, "zip": ZipUri}
FTP_ONLY = {"ftp": FtpPath}


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as handle:
        handle.writestr("m.txt", "member")
    return path


def test_a_construction_with_a_map_dispatches_from_the_map_only():
    assert type(UriPath("data:,x", schemesmap=ALLOW)) is DataUri
    assert type(UriPath("file:///tmp/x", schemesmap=ALLOW)) is UriPath
    assert type(UriPath("file:///tmp/x")) is FileUri


def test_a_zip_path_opens_its_outer_archive_through_the_map(archive):
    url = f"zip:{archive.as_uri()}!/m.txt"
    assert UriPath(url).read_text() == "member"
    allowing = dict(ALLOW, file=FileUri)
    assert UriPath(url, schemesmap=allowing).read_text() == "member"
    restricted = UriPath(url, schemesmap=ALLOW)
    assert type(restricted.backend.outer) is UriPath
    with pytest.raises(NotImplementedError):
        restricted.read_text()


def test_a_restricted_zip_path_does_not_borrow_a_handle_another_path_opened(archive):
    url = f"zip:{archive.as_uri()}!/m.txt"
    open_elsewhere = UriPath(url)
    assert open_elsewhere.read_text() == "member"
    restricted = UriPath(url, schemesmap=ALLOW)
    assert restricted.backend is not open_elsewhere.backend
    with pytest.raises(NotImplementedError):
        restricted.exists()


def test_a_derived_zip_path_keeps_the_map(archive):
    restricted = UriPath(f"zip:{archive.as_uri()}!/", schemesmap=ALLOW)
    member = restricted / "m.txt"
    assert member._schemes_in_use is ALLOW
    with pytest.raises(NotImplementedError):
        member.read_text()


def test_a_string_destination_with_a_scheme_is_dispatched_from_the_map(tmp_path):
    target = tmp_path / "out.txt"
    source = UriPath("data:,hello", schemesmap=ALLOW)
    with pytest.raises(NotImplementedError):
        source.copy(target.as_uri())
    assert not target.exists()
    assert type(source._coerce_target(target.as_uri())) is UriPath
    assert type(source._coerce_target("data:,x")) is DataUri
    # Without a map the same destination is a local file again.
    UriPath("data:,hello").copy(target.as_uri())
    assert target.read_text() == "hello"


def test_a_join_with_a_uri_argument_chooses_its_class_from_the_map():
    base = UriPath("ftp://h/dir", schemesmap=FTP_ONLY)
    other = UriPath("file:///tmp/x")
    assert type(base / other) is UriPath
    assert type(base.joinpath(other)) is UriPath
    assert type(base.parent / other) is UriPath
    assert (base / other)._schemes_in_use is FTP_ONLY
    assert type(UriPath("ftp://h/dir") / other) is FileUri
    assert type(base / "child") is FtpPath


def test_with_source_chooses_its_class_from_the_map():
    path = UriPath("ftp://h/dir", schemesmap=FTP_ONLY)
    moved = path.with_source(Source("file", None, "", None))
    assert type(moved) is UriPath
    assert moved._schemes_in_use is FTP_ONLY
    assert type(path.with_source(Source("ftp", None, "other", None))) is FtpPath
    assert type(UriPath("ftp://h/dir").with_source(Source("file", None, "", None))) is (
        FileUri
    )


def test_a_map_restricts_dispatch_and_nothing_a_built_class_does(tmp_path):
    local = FileUri((tmp_path / "f.txt").as_uri())
    restricted = UriPath("data:,y", schemesmap=ALLOW)
    # A path the caller built itself is theirs to use.
    restricted.copy(local)
    assert local.read_text() == "y"
