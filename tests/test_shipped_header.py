"""The shipped API header (`pathlib_next/AGENTS.md`) against the installed code.

Every fenced Python block of the header is a stub: `class`/`def` lines whose
parameter lists, `class` bases, and constant values are compared with the real
objects. A name is looked up in the modules named in the backticks of the
block's headings, innermost heading first. A default written `...` stands for
"a default exists" where its value has no literal spelling.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import re
import sys

import pytest

import pathlib_next

HEADER = pathlib.Path(pathlib_next.__file__).resolve().parent / "AGENTS.md"
MODULE = re.compile(r"`(pathlib_next[\w.]*)`")
KINDS = {
    "POSITIONAL_ONLY": inspect.Parameter.POSITIONAL_ONLY,
    "POSITIONAL_OR_KEYWORD": inspect.Parameter.POSITIONAL_OR_KEYWORD,
    "VAR_POSITIONAL": inspect.Parameter.VAR_POSITIONAL,
    "KEYWORD_ONLY": inspect.Parameter.KEYWORD_ONLY,
    "VAR_KEYWORD": inspect.Parameter.VAR_KEYWORD,
}


class Unavailable(Exception):
    """A module of the block cannot be imported (an extra is not installed)."""


def blocks(text):
    """`[(headings, modules, line, source)]` for every ```python fence."""
    found, stack, lines = [], [], text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        heading = re.match(r"(#+) (.*)", line)
        if heading:
            level = len(heading.group(1))
            stack = [h for h in stack if h[0] < level] + [(level, heading.group(2))]
        elif line.startswith("```"):
            end = index + 1
            while not lines[end].startswith("```"):
                end += 1
            if line == "```python":
                titles = [title for _level, title in stack]
                modules = [
                    m for title in reversed(titles) for m in MODULE.findall(title)
                ]
                found.append(
                    (titles, modules, index + 2, "\n".join(lines[index + 1 : end]))
                )
            index = end
        index += 1
    return found


def lookup(modules, name):
    for module in modules:
        try:
            imported = importlib.import_module(module)
        except ImportError as error:
            raise Unavailable(f"{module}: {error}") from None
        if hasattr(imported, name):
            return getattr(imported, name)
    raise AttributeError(f"{name} is in none of {modules}")


def stub_parameters(arguments, drop):
    """`[(name, kind, default node | None)]` of an `ast.arguments`."""
    positional = arguments.posonlyargs + arguments.args
    defaults = [None] * (len(positional) - len(arguments.defaults)) + list(
        arguments.defaults
    )
    found = [
        (
            a.arg,
            (
                "POSITIONAL_ONLY"
                if i < len(arguments.posonlyargs)
                else "POSITIONAL_OR_KEYWORD"
            ),
            d,
        )
        for i, (a, d) in enumerate(zip(positional, defaults))
    ]
    if arguments.vararg:
        found.append((arguments.vararg.arg, "VAR_POSITIONAL", None))
    found += [
        (a.arg, "KEYWORD_ONLY", d)
        for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults)
    ]
    if arguments.kwarg:
        found.append((arguments.kwarg.arg, "VAR_KEYWORD", None))
    return found[1:] if drop else found


def same_default(node, actual):
    if isinstance(node, ast.Constant) and node.value is Ellipsis:
        return True
    try:
        literal = ast.literal_eval(node)
    except ValueError:
        return ast.unparse(node) in (getattr(actual, "__name__", None), repr(actual))
    return type(literal) is type(actual) and literal == actual


def signature_problems(label, arguments, drop_stub, actual, drop_actual):
    expected = stub_parameters(arguments, drop_stub)
    parameters = list(actual.parameters.values())
    if drop_actual:
        parameters = parameters[1:]
    real = [(p.name, p.kind.name, p.default) for p in parameters]
    shown = lambda items: ", ".join(
        f"{n}{'=' if d is not None else ''}" for n, _k, d in items
    )  # noqa: E731
    if [(n, k) for n, k, _d in expected] != [(n, k) for n, k, _d in real]:
        return [
            f"{label}: header has ({shown(expected)}), code has "
            f"({', '.join(n for n, _k, _d in real)}) {[k for _n, k, _d in real]}"
        ]
    problems = []
    for (name, _kind, node), (_n, _k, default) in zip(expected, real):
        if (node is None) != (default is inspect.Parameter.empty):
            problems.append(
                f"{label}: {name} default is {'given' if node else 'absent'} in the header only"
            )
        elif node is not None and not same_default(node, default):
            problems.append(
                f"{label}: {name}={ast.unparse(node)} in the header, {default!r} in the code"
            )
    return problems


def check_class(modules, node):
    cls = lookup(modules, node.name)
    problems = []
    names = {c.__name__ for c in cls.__mro__}
    for base in node.bases:
        last = ast.unparse(base).rsplit(".", 1)[-1]
        if last == "NamedTuple" and issubclass(cls, tuple):
            continue
        if last not in names:
            problems.append(f"class {node.name}: {last} is not a base in the code")
    for member in node.body:
        if isinstance(member, ast.Expr) and isinstance(member.value, ast.Constant):
            continue
        if isinstance(member, (ast.AnnAssign, ast.Expr, ast.Assign)):
            target = (
                member.target
                if isinstance(member, ast.AnnAssign)
                else (
                    member.targets[0]
                    if isinstance(member, ast.Assign)
                    else member.value
                )
            )
            label = f"{node.name}.{target.id}"
            if not hasattr(cls, target.id) and target.id not in getattr(
                cls, "__annotations__", {}
            ):
                problems.append(f"{label}: no such attribute")
            elif isinstance(member, ast.Assign) and not (
                isinstance(member.value, ast.Constant)
                and member.value.value is Ellipsis
            ):
                if not same_default(member.value, getattr(cls, target.id)):
                    problems.append(
                        f"{label} = {ast.unparse(member.value)} in the header, {getattr(cls, target.id)!r} in the code"
                    )
            continue
        assert isinstance(member, ast.FunctionDef), ast.dump(member)
        label = f"{node.name}.{member.name}"
        if member.name == "__init__":
            problems += signature_problems(
                label, member.args, True, inspect.signature(cls), False
            )
            continue
        if not hasattr(cls, member.name):
            problems.append(f"{label}: no such method")
            continue
        decorators = [ast.unparse(d) for d in member.decorator_list]
        raw = inspect.getattr_static(cls, member.name)
        wanted = (
            "classmethod"
            if "classmethod" in decorators
            else "staticmethod" if "staticmethod" in decorators else None
        )
        found = (
            "classmethod"
            if isinstance(raw, classmethod)
            else "staticmethod" if isinstance(raw, staticmethod) else None
        )
        if wanted != found:
            problems.append(
                f"{label}: {wanted or 'an instance method'} in the header, {found or 'an instance method'} in the code"
            )
        bound = getattr(cls, member.name)
        actual = inspect.signature(bound)
        plain = inspect.isfunction(bound) and found is None
        problems += signature_problems(
            label, member.args, wanted != "staticmethod", actual, plain
        )
    return problems


def check_block(modules, source):
    problems = []
    tree = ast.parse(source)
    if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in tree.body):
        return problems  # an example that imports; another test runs it
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            problems += check_class(modules, node)
        elif isinstance(node, ast.FunctionDef):
            problems += signature_problems(
                node.name,
                node.args,
                False,
                inspect.signature(lookup(modules, node.name)),
                False,
            )
        elif isinstance(node, ast.Assign):
            name = node.targets[0].id
            actual = lookup(modules, name)
            if isinstance(node.value, ast.Name):
                if actual is not lookup(modules, node.value.id):
                    problems.append(f"{name} is not {node.value.id} in the code")
            elif not same_default(node.value, actual):
                problems.append(
                    f"{name} = {ast.unparse(node.value)} in the header, {actual!r} in the code"
                )
        else:
            raise AssertionError(
                f"unsupported statement in a header block: {ast.dump(node)}"
            )
    return problems


def header_problems(text):
    """Every difference between the stubs of `text` and the code, as lines."""
    found = []
    for titles, modules, line, source in blocks(text):
        try:
            found += [
                f"line {line} ({titles[-1]}): {p}" for p in check_block(modules, source)
            ]
        except Unavailable:
            pass
        except AttributeError as error:
            found.append(f"line {line} ({titles[-1]}): {error}")
    return found


def _cases():
    for titles, modules, line, source in blocks(HEADER.read_text(encoding="utf-8")):
        yield pytest.param(modules, source, id=f"{line}:{titles[-1][:40]}")


@pytest.mark.parametrize(("modules", "source"), list(_cases()))
def test_the_stubs_of_each_block_match_the_code(modules, source):
    try:
        problems = check_block(modules, source)
    except Unavailable as error:
        pytest.skip(f"an extra is not installed: {error}")
    assert problems == []


def test_the_header_has_blocks_for_both_layers():
    found = blocks(HEADER.read_text(encoding="utf-8"))
    assert len(found) > 40
    assert {m for _t, mods, _l, _s in found for m in mods} >= {
        "pathlib_next.path",
        "pathlib_next.utils.sync",
        "pathlib_next.uri",
        "pathlib_next.uri.schemes.sftp",
    }


# --- the checker itself: an altered stub is reported ---


@pytest.mark.parametrize(
    ("old", "new", "report"),
    [
        (
            "follow_binds=False,",
            "follow_binds=True,",
            "follow_binds=True in the header",
        ),
        ("def rmdir(self): ...", "def rmdir(self, parents=False): ...", "rmdir"),
        (
            "def move(self, target, *, overwrite=False)",
            "def move(self, target, overwrite=False)",
            "move",
        ),
        (
            "class Path(Pathname, Chmod, Stat, BinaryOpen):",
            "class Path(Pathname, Elsewhere):",
            "Elsewhere is not a base",
        ),
        (
            "def move_into(self, target_dir",
            "def move_here(self, target_dir",
            "move_here: no such method",
        ),
    ],
)
def test_the_checker_reports_an_altered_stub(old, new, report):
    text = HEADER.read_text(encoding="utf-8")
    assert old in text
    assert header_problems(text) == []
    altered = text.replace(old, new, 1)
    problems = header_problems(altered)
    assert any(report in problem for problem in problems), problems


# --- the shape the header's standard asks for ---


def _headings(text, level):
    return [m.group(1) for m in re.finditer(rf"(?m)^{'#' * level} (.+)$", text)]


def test_the_header_closes_with_the_four_standard_sections():
    text = HEADER.read_text(encoding="utf-8")
    assert _headings(text, 2)[-4:] == [
        "Exceptions",
        "Command line",
        "Environment variables",
        "Gotchas",
    ]
    for section in ("Exceptions", "Gotchas"):
        body = text.split(f"\n## {section}\n", 1)[1].split("\n## ", 1)[0]
        assert re.search(r"(?m)^### Core\b", body)
        assert re.search(r"(?m)^### URI layer\b", body)


def test_the_header_opens_as_the_standard_says():
    text = HEADER.read_text(encoding="utf-8")
    assert text.startswith("# `pathlib_next` — public API header\n")
    opening = text.split("\n\n")[1]
    assert "ships inside the package" in opening and "self-contained" in opening
    assert "<https://github.com/jose-pr/pathlib-next>" in opening
    assert "`pathlib_next.__version__`" in text.split("\n## ", 1)[0]


def test_the_header_links_only_to_absolute_urls():
    text = HEADER.read_text(encoding="utf-8")
    assert re.findall(r"\]\((?!https?://)[^)]*\)", text) == []
    for private in (".agents", "CHANGELOG.md", "docs/", "src/pathlib_next"):
        assert private not in text


def test_each_layer_is_one_contiguous_run_of_sections():
    text = HEADER.read_text(encoding="utf-8")
    titles = _headings(text, 2)
    uri = [
        i for i, t in enumerate(titles) if t.startswith(("URIs", "Built-in schemes"))
    ]
    assert uri == list(range(uri[0], uri[0] + len(uri)))
    core = titles[: uri[0]]
    assert core[0].startswith("Package root") and core[-1].startswith("Testing helpers")


# --- the environment variable the header documents ---


@pytest.fixture
def sftp(monkeypatch):
    module = pytest.importorskip("pathlib_next.uri.schemes.sftp")
    monkeypatch.setattr(module, "_resolved_backend_cls", None)
    return module


def test_the_variable_in_the_header_is_the_one_the_code_reads(sftp):
    section = HEADER.read_text(encoding="utf-8").split("\n## Environment variables\n")[
        1
    ]
    assert sftp._ENV_VAR == "PATHLIB_NEXT_SFTP_BACKEND"
    assert f"### `{sftp._ENV_VAR}`" in section


@pytest.mark.parametrize("value", ["", "Paramiko", "ASYNCSSH", "other"])
def test_an_empty_or_unrecognised_or_differently_cased_value_is_an_error(
    sftp, monkeypatch, value
):
    monkeypatch.setenv(sftp._ENV_VAR, value)
    with pytest.raises(ValueError, match="PATHLIB_NEXT_SFTP_BACKEND"):
        sftp._resolve_default_backend_cls()
    assert sftp._resolved_backend_cls is None


def test_an_unset_variable_is_auto(sftp, monkeypatch):
    monkeypatch.delenv(sftp._ENV_VAR, raising=False)
    try:
        chosen = sftp._resolve_default_backend_cls()
    except ImportError:
        pytest.skip("neither SSH library is installed")
    monkeypatch.setattr(sftp, "_resolved_backend_cls", None)
    monkeypatch.setenv(sftp._ENV_VAR, "auto")
    assert sftp._resolve_default_backend_cls() is chosen


def test_the_resolved_backend_is_kept_for_the_life_of_the_process(sftp, monkeypatch):
    kept = type("Kept", (), {})
    monkeypatch.setattr(sftp, "_resolved_backend_cls", kept)
    monkeypatch.setenv(sftp._ENV_VAR, "other")
    assert sftp._resolve_default_backend_cls() is kept


def test_a_named_library_that_is_not_installed_is_an_import_error(sftp, monkeypatch):
    monkeypatch.setattr(sftp, "_BACKEND_REGISTRY", {})
    monkeypatch.setattr(sftp, "_paramiko_probed", False)
    monkeypatch.setitem(sys.modules, sftp.__name__ + "._paramiko", None)
    monkeypatch.setenv(sftp._ENV_VAR, "paramiko")
    with pytest.raises(ImportError, match="sftp"):
        sftp._resolve_default_backend_cls()
