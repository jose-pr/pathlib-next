"""An `archive:` path decides its format on first use, from the bytes it is
about to read anyway, and building, printing, joining or pickling a path
reaches nothing. A loopback HTTP server logs what the outer archive costs."""

import copy
import http.server
import io
import pickle
import tarfile
import threading
import zipfile

import pytest

pytest.importorskip("uritools")
pytest.importorskip("requests")

from pathlib_next.uri import UriPath


def _zip_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _tar_bytes(members, mode="w"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _Site:
    """Serves `files` (path -> bytes), logs every request, and answers 503
    while `failing`."""

    def __init__(self, files):
        self.files = files
        self.log = []
        self.failing = False
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                site.log.append(f"GET {self.path}")
                body = site.files.get(self.path)
                status = 503 if site.failing else (404 if body is None else 200)
                if status != 200:
                    body = b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.05,), daemon=True
        )

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def site():
    files = {
        "/noext": _zip_bytes({"m": b"M", "d/n": b"N"}),
        "/tarnoext": _tar_bytes({"m": b"T"}),
        "/a.zip": _zip_bytes({"m": b"M"}),
    }
    with _Site(files) as served:
        yield served


# --- building a path reaches nothing -----------------------------------------------------


@pytest.mark.parametrize("scheme", ["archive", "archive+zip", "zip", "tar"])
def test_building_printing_joining_and_pickling_a_path_send_no_request(site, scheme):
    path = UriPath(f"{scheme}:{site.url}/noext!/d/n")
    text = (
        repr(path),
        str(path),
        path.as_uri(),
        path.name,
        path.parent,
        path.with_name("x"),
        path / "y",
        path.parent / "z",
        path == UriPath(f"{scheme}:{site.url}/noext!/d/n"),
        hash(path),
        copy.copy(path),
        copy.deepcopy(path),
        pickle.loads(pickle.dumps(path)),
    )
    assert text[0] and site.log == []


# --- the first use settles the format, and costs one request ---------------------------------


def test_the_first_read_of_an_undecided_zip_fetches_the_outer_once(site):
    path = UriPath(f"archive:{site.url}/noext!/m")
    assert path.read_bytes() == b"M"
    assert site.log == ["GET /noext"]
    assert (path.parent / "d" / "n").read_bytes() == b"N"
    assert site.log == ["GET /noext"]


def test_the_first_read_of_an_undecided_tar_fetches_the_outer_once(site):
    path = UriPath(f"archive:{site.url}/tarnoext!/m")
    assert path.read_bytes() == b"T"
    assert site.log == ["GET /tarnoext"]


def test_an_undecided_archive_that_is_not_there_costs_one_request(site):
    path = UriPath(f"archive:{site.url}/gone!/m")
    assert not path.exists()
    assert site.log == ["GET /gone"]
    with pytest.raises(FileNotFoundError):
        path.read_bytes()


def test_a_format_named_by_the_scheme_or_the_extension_is_fetched_once(site):
    assert UriPath(f"zip:{site.url}/a.zip!/m").read_bytes() == b"M"
    assert site.log == ["GET /a.zip"]
    site.log.clear()
    assert UriPath(f"archive:{site.url}/a.zip!/m").read_bytes() == b"M"
    assert site.log == ["GET /a.zip"]


def test_a_failure_to_read_the_outer_decides_nothing_for_the_next_try(site):
    site.failing = True
    path = UriPath(f"archive:{site.url}/noext!/m")
    with pytest.raises(OSError):
        path.read_bytes()
    site.failing = False
    assert path.read_bytes() == b"M"
    assert path.backend is UriPath(f"zip:{site.url}/noext!/m").backend


def test_an_undecided_local_archive_is_read_by_what_it_holds(tmp_path):
    archive = tmp_path / "blob"
    archive.write_bytes(_zip_bytes({"m": b"local"}))
    path = UriPath(f"archive:{archive.as_uri()}!/m")
    assert path.read_bytes() == b"local"
    assert path.backend is UriPath(f"zip:{archive.as_uri()}!/m").backend


# --- a path pickled and loaded again reaches the same handle --------------------------


@pytest.mark.parametrize("scheme", ["zip", "archive", "archive+zip"])
@pytest.mark.parametrize("protocol", [2, pickle.HIGHEST_PROTOCOL])
def test_a_loaded_path_shares_the_handle_the_original_holds(tmp_path, scheme, protocol):
    archive = tmp_path / "a.zip"
    archive.write_bytes(_zip_bytes({"m": b"M"}))
    path = UriPath(f"{scheme}:{archive.as_uri()}!/m")
    again = pickle.loads(pickle.dumps(path, protocol))
    assert again == path and str(again) == str(path)
    assert again.backend is path.backend
    assert copy.deepcopy(path).backend is path.backend
    assert again.read_bytes() == b"M"


def test_a_tar_path_and_a_nested_archive_path_share_their_handles_after_a_pickle(
    tmp_path,
):
    outer = tmp_path / "o.tar"
    outer.write_bytes(_tar_bytes({"inner.zip": _zip_bytes({"x": b"X"}), "m": b"M"}))
    plain = UriPath(f"tar:{outer.as_uri()}!/m")
    nested = UriPath(f"zip:tar:{outer.as_uri()}!/inner.zip!/x")
    for path in (plain, nested):
        again = pickle.loads(pickle.dumps(path))
        assert again == path
        assert again.backend is path.backend
    assert pickle.loads(pickle.dumps(nested)).read_bytes() == b"X"


# --- a held remote archive is read again on request -------------------------------------


def test_refresh_makes_a_remote_archive_that_changed_readable(site):
    path = UriPath(f"zip:{site.url}/a.zip!/m")
    other = UriPath(f"zip:{site.url}/a.zip!/")
    assert path.read_bytes() == b"M"
    site.files["/a.zip"] = _zip_bytes({"m": b"CHANGED", "new": b"N"})
    # The bytes of a remote outer are kept while any path to it lives.
    assert path.read_bytes() == b"M" and not (other / "new").exists()
    path.refresh()
    assert site.log == ["GET /a.zip"]  # forgetting costs nothing by itself
    assert path.read_bytes() == b"CHANGED"
    assert (other / "new").read_bytes() == b"N"  # every path to it shares the refresh
    assert site.log == ["GET /a.zip", "GET /a.zip"]


def test_refresh_of_an_undecided_archive_nothing_read_yet_sends_nothing(site):
    path = UriPath(f"archive:{site.url}/noext!/m")
    path.refresh()
    assert site.log == []
    assert path.read_bytes() == b"M"
    site.files["/noext"] = _zip_bytes({"m": b"CHANGED"})
    path.refresh()
    assert path.read_bytes() == b"CHANGED"


def test_refresh_of_a_local_archive_changes_nothing(tmp_path):
    archive = tmp_path / "a.zip"
    archive.write_bytes(_zip_bytes({"m": b"M"}))
    path = UriPath(f"zip:{archive.as_uri()}!/m")
    assert path.read_bytes() == b"M"
    path.refresh()
    assert path.read_bytes() == b"M"


@pytest.mark.parametrize("scheme", ["archive", "zip", "archive+zip"])
def test_two_paths_built_for_one_archive_are_on_one_filesystem(tmp_path, scheme):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("a.txt", "A")
    first = UriPath(f"{scheme}:{archive.as_uri()}!/") / "a.txt"
    second = UriPath(f"{scheme}:{archive.as_uri()}!/") / "a.txt"
    other_spelling = UriPath(f"zip:{archive.as_uri()}!/") / "a.txt"

    assert first._same_filesystem(second) and second._same_filesystem(first)
    assert first._same_filesystem(other_spelling)
    assert pickle.loads(pickle.dumps(first))._same_filesystem(first)
    with pytest.raises(OSError):
        first.copy(second, overwrite=True)
    assert first.read_bytes() == b"A"


def test_paths_of_two_archives_are_on_two_filesystems(tmp_path):
    paths = []
    for name in ("a.zip", "b.zip"):
        archive = tmp_path / name
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("a.txt", "A")
        paths.append(UriPath(f"archive:{archive.as_uri()}!/") / "a.txt")

    assert not paths[0]._same_filesystem(paths[1])


def test_an_archive_that_cannot_be_read_is_on_no_other_filesystem(tmp_path):
    missing = UriPath(f"archive:{(tmp_path / 'missing').as_uri()}!/") / "a.txt"
    again = UriPath(f"archive:{(tmp_path / 'missing').as_uri()}!/") / "a.txt"

    assert not missing._same_filesystem(again)
