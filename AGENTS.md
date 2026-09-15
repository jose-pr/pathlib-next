# pathlib_next — contributor orientation

Orientation for working in a checkout of this repository: layout, environments,
commands, CI and release. It is not the API reference and it does not ship.

- **Public API contract** (every export, signature and gotcha):
  [`src/pathlib_next/AGENTS.md`](src/pathlib_next/AGENTS.md). That file ships in
  the wheel, so it must stay self-contained (no repo-relative links) and must be
  updated in the same commit as any public API change.
- **Deliberate differences from `pathlib.Path`**:
  [`docs/divergences.md`](docs/divergences.md). `pathlib.Path` parity is the
  contract; a behavioral divergence that is not recorded there is a bug.

## Layout

| Path | Contents |
| --- | --- |
| `src/pathlib_next/` | The package (`src/` layout). `py.typed` and the API header ship with it. |
| `src/pathlib_next/uri/schemes/` | One module per URI scheme; registered through the `pathlib_next.schemes` entry points in `pyproject.toml`. |
| `tests/` | The pytest suite. |
| `benchmarks/` | `bench.py` and committed JSON results; see [`benchmarks/README.md`](benchmarks/README.md). Not shipped. |
| `examples/` | Runnable scripts. Networked ones skip (exit 0) unless their environment variables are set. |
| `docs/`, `mkdocs.yml` | MkDocs site: hand-written pages plus a `mkdocstrings` API reference. |
| `CHANGELOG.md` | Keep a Changelog. Released sections are frozen records. |

## Environments

Python 3.9 is the floor (`requires-python = ">=3.9"`) and 3.14 is the latest
supported. Test on both ends before claiming a change works.

Keep one virtualenv per interpreter under `.venv/<version>-<os>-<arch>/`
(gitignored), where `<os>` is `os.name` (`nt`/`posix`) or `darwin`, and `<arch>`
is the architecture the interpreter was built for:

```bash
python -m venv .venv/3.14-posix-x86_64
.venv/3.14-posix-x86_64/bin/python -m pip install -e ".[dev,docs,uri,http,sftp,sftp-async,s3,gs,az]"
```

On Windows the interpreter is `.venv\<name>\Scripts\python.exe`.

Install every extra that has tests. A missing extra does not fail the suite:
its tests are skipped instead, so check `pytest -rs` before trusting a green run.
The `gs`/`az` SDKs are the ones most likely to be unavailable for an older
interpreter or a less common platform; without them their contract suites skip.

## Everyday commands

```bash
python -m pytest -q                                  # full suite (pythonpath=src is configured)
python -m pytest -q --cov=pathlib_next --cov-report=term-missing
python -m black src/ tests/ benchmarks/ examples/    # formatting; --check to verify
mkdocs build --strict                                # docs must build with no warnings
python -m build                                      # sdist + wheel into dist/
```

- **Formatting is black**, pinned to `target-version = ["py39"]` so it never
  emits syntax the floor cannot parse. No linter or type checker is enforced.
- **Every file is LF** (`.gitattributes` sets `* text=auto eol=lf`). On Windows,
  black writes CRLF: convert the files back to LF after formatting and check
  `git diff --stat` for whitespace-only churn.
- **3.9 compatibility**: any module using `X | Y` in a runtime-evaluated
  annotation needs `from __future__ import annotations`.
- **`*.local.*` files** are per-machine overrides: gitignored and excluded from
  both build targets. Keep hostnames and credentials in those, never in tracked
  files.

## Benchmarks

`python benchmarks/bench.py --help` lists the suites. `--save` writes a JSON
result per (version, interpreter, platform) into `benchmarks/results/`; the
schema and the reproduce command are in
[`benchmarks/README.md`](benchmarks/README.md). A local run is a sanity check;
performance claims in the changelog or release notes come from CI runs.

## CI

Workflows live in `.github/workflows/`:

- `test.yml` runs on `workflow_dispatch` (optional `ref` input) or on a pushed
  `ci-*` tag, never on ordinary pushes. To test a commit without the dashboard,
  push a uniquely named throwaway tag (`ci-<topic>-<timestamp>`), follow the run
  to completion, then delete the tag locally and on the remote.
- `release.yml` runs on a `v*` tag: test gate, build, PyPI publish through
  Trusted Publishing, and a GitHub release whose notes come from that
  version's `CHANGELOG.md` section. Its docs job only checks that the site
  builds strictly; it never deploys.
- `docs.yml` owns every GitHub Pages deploy: on a published release, on a push
  to `main` that touches the docs sources, and on `workflow_dispatch`.

## Releasing

1. Move the `[Unreleased]` entries under a new `## [x.y.z] - <date>` heading and
   add its link definition at the bottom of `CHANGELOG.md`.
2. Bump `version` in `pyproject.toml` in the same commit (PEP 440 syntax there;
   SemVer in tags and the changelog).
3. Before 1.0, bump the minor only when the documented API breaks. New methods,
   new optional arguments and fixes are patch releases.
4. Run the full suite on the floor and latest interpreters, `mkdocs build
   --strict`, `python -m build`, and the maintainer's leak check (a scan for
   private references and agent-attribution commit trailers). Judge it by exit
   code.
5. Push `main`, then the `v*` tag. Publishing is irreversible, so the tag is
   pushed only with the maintainer's explicit consent for that specific
   release.

Changelog entries say what changed and what a user must do about it. They do
not describe how the work was done.

## Commits

- Logical commits in `type: description` form (`feat:`, `fix:`, `docs:`,
  `chore:`); keep code with its tests, and docs/config/CI in separate commits.
- No agent attribution in commit messages: no `Co-Authored-By:` naming a model
  or assistant, no `*-Session:` trailers, no session URLs, no "generated with"
  footers.
