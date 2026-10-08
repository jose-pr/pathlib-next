"""The fenced Python blocks of README.md, docs/index.md and the extending guide
run, in document order and in one scratch directory per document, each as its
own `python -W error` process.

A block that cannot run on its own is marked by an HTML comment on the line
above its fence: `<!-- example: skip: <reason> -->` (a remote host, a method
fragment, a pytest module that another test already runs). Every block is
either run or marked, so a new example cannot be left out by accident.
"""

import os
import pathlib
import re
import subprocess
import sys

import pytest

pytest.importorskip("uritools")

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCUMENTS = ["README.md", "docs/index.md", "docs/guides/extending.md"]
SKIP = re.compile(r"^<!--\s*example:\s*skip:\s*(?P<reason>\S.*?)\s*-->$")
FENCE = re.compile(r"^```(?P<info>\S*)")


def python_blocks(text):
    """`[(line number, source, skip reason or None)]` for every ```python
    fence of `text`."""
    lines = text.splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        fence = FENCE.match(lines[index])
        if not fence:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("```"):
            end += 1
        if fence.group("info") == "python":
            marker = SKIP.match(lines[index - 1].strip()) if index else None
            blocks.append(
                (
                    index + 1,
                    "\n".join(lines[index + 1 : end]) + "\n",
                    marker.group("reason") if marker else None,
                )
            )
        index = end + 1
    return blocks


def _cases():
    for document in DOCUMENTS:
        text = (REPO / document).read_text(encoding="utf-8")
        for line, source, reason in python_blocks(text):
            marks = [pytest.mark.skip(reason=reason)] if reason else []
            yield pytest.param(
                document, line, source, marks=marks, id=f"{document}:{line}"
            )


@pytest.fixture(scope="module")
def workdirs(tmp_path_factory):
    made = {}

    def workdir(document):
        if document not in made:
            made[document] = tmp_path_factory.mktemp(document.replace("/", "_"))
        return made[document]

    return workdir


@pytest.mark.parametrize("document, line, source", list(_cases()))
def test_the_example_runs(document, line, source, workdirs):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO / "src"), env.get("PYTHONPATH")])
    )
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", source],
        cwd=workdirs(document),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"{document}:{line}\n{result.stdout}{result.stderr}"
    assert "Traceback" not in result.stderr


def test_every_document_has_python_blocks_and_each_is_run_or_marked():
    for document in DOCUMENTS:
        text = (REPO / document).read_text(encoding="utf-8")
        assert python_blocks(text), document


def test_a_marker_needs_a_reason_and_sits_directly_above_its_fence():
    blocks = python_blocks(
        "<!-- example: skip: a remote host -->\n```python\nx = 1\n```\n"
        "\n<!-- example: skip: -->\n```python\ny = 2\n```\n"
        "<!-- example: skip: far away -->\n\n```python\nz = 3\n```\n"
        "```text\nnot python\n```\n```python\nw = 4\n```\n"
    )
    assert [(line, reason) for line, _source, reason in blocks] == [
        (2, "a remote host"),
        (7, None),
        (12, None),
        (18, None),
    ]
    assert blocks[0][1] == "x = 1\n"
