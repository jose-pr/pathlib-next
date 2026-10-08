"""The public surface: what each module exports, the version, and the
constructors as a type checker reads them."""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

import pathlib_next

PACKAGE = pathlib.Path(pathlib_next.__file__).resolve().parent
HEADER = PACKAGE / "AGENTS.md"

# Each module's exports, as the shipped header documents them.
EXPORTS = {
    "pathlib_next": {
        "BinaryOpen",
        "Chmod",
        "FileStat",
        "FsPathLike",
        "LocalPath",
        "Path",
        "PathLike",
        "Pathname",
        "PosixPathname",
        "PurePathLike",
        "Stat",
        "WindowsPathname",
        "__version__",
        "glob",
        "sync",
    },
    "pathlib_next.path": {"FsPathLike", "Path", "PathLike", "Pathname", "PurePathLike"},
    "pathlib_next.fspath": {"LocalPath", "PosixPathname", "WindowsPathname"},
    "pathlib_next.protocols": {
        "BinaryOpen",
        "Chmod",
        "FileStatLike",
        "NativeChecksum",
        "Stat",
    },
    "pathlib_next.protocols.fs": {"Chmod", "FileStatLike", "Stat"},
    "pathlib_next.protocols.io": {"BinaryOpen"},
    "pathlib_next.protocols.checksum": {"NativeChecksum"},
    "pathlib_next.utils": {
        "LRU",
        "UNCHANGED",
        "as_error_handler",
        "as_mode",
        "as_owner",
        "is_safe_child_name",
        "is_windows_flavoured",
        "make_archive",
        "md5",
        "notimplemented",
        "parsedate",
        "sha256",
        "sizeof_fmt",
        "unpack_archive",
    },
    "pathlib_next.utils.stat": {"FileStat"},
    "pathlib_next.utils.glob": {
        "NonRelativePatternError",
        "RECURSIVE",
        "full_match",
        "glob",
        "parse_pattern",
        "select",
    },
}

# Documented too, in modules that declare no `__all__` of their own.
ALSO_IMPORTABLE = {
    "pathlib_next.mempath": {"MemPath", "MemPathBackend", "MemFile"},
    "pathlib_next.utils.sync": {"PathSyncer", "SyncEvent", "PathAndStat"},
    "pathlib_next.utils.checksum": {"md5", "sha256", "stream", "native"},
    "pathlib_next.utils.archive": {"make_archive", "unpack_archive"},
    "pathlib_next.testing": {
        "DIRECTORY_ERRORS",
        "FIXTURE_TREE",
        "NOT_EMPTY_ERRNOS",
        "PathContract",
        "PurePathContract",
        "ReadPathContract",
        "populate_fixture_tree",
    },
    "pathlib_next.uri": {"Uri", "UriPath"},
}


def _uri_available():
    try:
        importlib.import_module("pathlib_next.uri")
    except ImportError:
        return False
    return True


@pytest.mark.parametrize("module", sorted(EXPORTS))
def test_module_declares_exactly_the_documented_exports(module):
    imported = importlib.import_module(module)
    declared = set(imported.__all__)
    expected = set(EXPORTS[module])
    if module == "pathlib_next" and _uri_available():
        expected |= {"Uri", "UriPath"}
    assert declared == expected
    assert len(imported.__all__) == len(declared)
    for name in declared:
        assert hasattr(imported, name), name


@pytest.mark.parametrize("module", sorted({**EXPORTS, **ALSO_IMPORTABLE}))
def test_documented_names_stay_importable_from_where_they_are_documented(module):
    names = EXPORTS.get(module, set()) | ALSO_IMPORTABLE.get(module, set())
    if module == "pathlib_next.uri" and not _uri_available():
        pytest.skip("the uri extra is not installed")
    imported = importlib.import_module(module)
    missing = sorted(name for name in names if not hasattr(imported, name))
    assert missing == []


def test_star_import_of_the_root_publishes_only_the_declared_names():
    namespace: dict = {}
    exec("from pathlib_next import *", namespace)
    published = {name for name in namespace if name != "__builtins__"}
    assert published == set(pathlib_next.__all__)
    for leaked in ("P", "PN", "annotations", "fspath", "path", "protocols", "utils"):
        assert leaked not in published


def test_names_that_were_never_documented_stay_importable_by_name():
    from pathlib_next.path import P, PN  # noqa: F401
    from pathlib_next.utils import K, V  # noqa: F401


def _header_import_lines():
    text = HEADER.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    for block in blocks:
        for line in block.splitlines():
            if re.match(r"(from \S+ import |import )", line):
                yield line


def test_the_import_lines_of_the_header_run():
    lines = list(_header_import_lines())
    assert any("pathlib_next.testing" in line for line in lines)
    for line in lines:
        exec(line, {})


def test_version_is_the_installed_distribution_version():
    assert isinstance(pathlib_next.__version__, str)
    try:
        installed = importlib.metadata.version("pathlib_next")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("the package is not installed")
    assert pathlib_next.__version__ == installed


def test_version_of_a_source_tree_that_is_not_installed():
    code = (
        "import importlib.metadata as m\n"
        "def missing(name):\n"
        "    raise m.PackageNotFoundError(name)\n"
        "m.version = missing\n"
        "import pathlib_next\n"
        "print(pathlib_next.__version__)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(PACKAGE.parent))
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.strip() == "0+unknown"


def _is_trivial_body(function: ast.FunctionDef) -> bool:
    """mypy's rule for a body that makes a protocol member abstract: nothing
    but a docstring, `...`, `pass` or `raise NotImplementedError`."""
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body:
        return True
    if len(body) > 1:
        return False
    (statement,) = body
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Expr):
        return isinstance(statement.value, ast.Constant) and (
            statement.value.value is Ellipsis
        )
    if isinstance(statement, ast.Raise) and statement.exc is not None:
        exc = statement.exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


def _protocol_stubs():
    for relative in (
        "path.py",
        "protocols/fs.py",
        "protocols/io.py",
        "protocols/checksum.py",
    ):
        tree = ast.parse((PACKAGE / relative).read_text(encoding="utf-8"))
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            bases = {
                getattr(base, "attr", getattr(base, "id", None)) for base in cls.bases
            }
            if "Protocol" not in bases:
                continue
            for function in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
                decorators = {
                    getattr(d, "attr", getattr(d, "id", None))
                    for d in function.decorator_list
                }
                if "abstractmethod" in decorators or "_abstract" in decorators:
                    continue
                yield f"{relative}:{cls.name}.{function.name}", function


def test_no_protocol_stub_has_a_body_a_type_checker_reads_as_abstract():
    stubs = list(_protocol_stubs())
    assert len(stubs) > 10
    trivial = [label for label, function in stubs if _is_trivial_body(function)]
    assert trivial == []


CONSUMER = """\
from pathlib_next import FileStat, LocalPath, Path
from pathlib_next.mempath import MemPath

bare = Path("x")
local = LocalPath("x")
memory = MemPath("/a")
stat = FileStat(st_size=1, is_dir=False)
joined = bare / "y"
"""


def _consumer(tmp_path):
    # Outside any dot-directory: pyright silently skips those.
    directory = tmp_path / "consumer"
    directory.mkdir()
    (directory / "consumer.py").write_text(CONSUMER, encoding="utf-8")
    return directory


def test_mypy_accepts_the_constructors(tmp_path, monkeypatch):
    api = pytest.importorskip("mypy.api")
    directory = _consumer(tmp_path)
    monkeypatch.setenv("MYPYPATH", str(PACKAGE.parent))
    out, err, status = api.run(
        [
            "--follow-imports=silent",
            "--no-incremental",
            "--cache-dir=" + str(tmp_path / "mypy-cache"),
            str(directory / "consumer.py"),
        ]
    )
    assert status == 0, out + err


def test_pyright_accepts_the_constructors(tmp_path):
    checker = shutil.which("pyright") or shutil.which("basedpyright")
    if checker is None:
        pytest.skip("pyright is not installed")
    directory = _consumer(tmp_path)
    (directory / "pyrightconfig.json").write_text(
        json.dumps(
            {"typeCheckingMode": "standard", "extraPaths": [str(PACKAGE.parent)]}
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [checker, "--pythonpath", sys.executable, "consumer.py"],
        capture_output=True,
        text=True,
        cwd=directory,
    )
    assert result.returncode == 0, result.stdout + result.stderr
