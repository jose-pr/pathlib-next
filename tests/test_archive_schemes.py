"""`archive+zip:` and `archive+tar:` are second names of `zip:` and `tar:`:
every scheme name, class name and printed form a path had stays, and the one
name rule serves every spelling of a member."""

import importlib
import io
import pickle
import tarfile
import zipfile

import pytest

pytest.importorskip("uritools")

from pathlib_next.uri import UriPath
from pathlib_next.uri.schemes import archive
from pathlib_next.uri.schemes.archive import (
    ArchiveTarUri,
    ArchiveUri,
    ArchiveZipUri,
    TarUri,
    ZipUri,
)

SCHEMES = {
    "zip": ZipUri,
    "archive+zip": ZipUri,
    "tar": TarUri,
    "archive+tar": TarUri,
    "archive": ArchiveUri,
}


def test_the_documented_class_names_are_still_importable():
    assert archive.ArchiveZipUri is ZipUri
    assert archive.ArchiveTarUri is TarUri
    assert ArchiveZipUri is ZipUri and ArchiveTarUri is TarUri
    for entry_point in (
        "pathlib_next.uri.schemes.archive:ArchiveZipUri",
        "pathlib_next.uri.schemes.archive:ArchiveTarUri",
    ):
        module, _, name = entry_point.partition(":")
        assert getattr(importlib.import_module(module), name) in (ZipUri, TarUri)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_every_registered_scheme_name_builds_its_class(scheme):
    path = UriPath(f"{scheme}:file:///a/outer.zip!/m")
    assert type(path) is SCHEMES[scheme]
    assert path.source.scheme == scheme


@pytest.mark.parametrize("scheme", SCHEMES)
def test_a_path_prints_and_pickles_with_the_scheme_it_was_written_with(scheme):
    path = UriPath(f"{scheme}:file:///a/outer.zip!/d/m")
    assert str(path).startswith(f"{scheme}:")
    assert path.as_uri().startswith(f"{scheme}:")
    again = pickle.loads(pickle.dumps(path))
    assert type(again) is type(path)
    assert again.source.scheme == scheme and str(again) == str(path)
    assert (path / "x").as_uri().startswith(f"{scheme}:")


def test_a_member_has_one_identity_under_the_pinned_and_the_plain_scheme(tmp_path):
    archive_path = tmp_path / "a.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("m", b"M")
    uri = archive_path.as_uri()
    one = UriPath(f"zip:{uri}!/m")
    two = UriPath(f"archive+zip:{uri}!/m")
    assert one != two  # two spellings of the URI
    assert one._node_key() == two._node_key()
    assert one.backend is two.backend
    assert one.read_bytes() == two.read_bytes() == b"M"


# --- hard links in a tar ----------------------------------------------------------


@pytest.mark.parametrize(
    "linkname", ["real.txt", "./real.txt", "d//real.txt", "d/./real.txt"]
)
def test_a_hard_link_reports_the_size_of_its_target_however_the_target_is_spelled(
    tmp_path, linkname
):
    archive_path = tmp_path / "links.tar"
    target = "d/real.txt" if linkname.startswith("d") else "real.txt"
    with tarfile.open(archive_path, "w") as tf:
        info = tarfile.TarInfo(target)
        info.size = 4
        tf.addfile(info, io.BytesIO(b"data"))
        link = tarfile.TarInfo("link.txt")
        link.type = tarfile.LNKTYPE
        link.linkname = linkname
        tf.addfile(link)
    path = UriPath(f"tar:{archive_path.as_uri()}!/link.txt")
    assert path.stat().st_size == len(path.read_bytes()) == 4
