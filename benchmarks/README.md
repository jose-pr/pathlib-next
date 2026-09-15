# Benchmarks

`bench.py` times `pathlib_next` hot paths: URI parsing, path joins, `MemPath`
glob, `LocalPath` against `pathlib.Path`, HTTP traversal and listing parsing,
and `paramiko` against `asyncssh` on a loopback SFTP server. Method notes and
past snapshots are on the docs site's Benchmarks page
([`docs/benchmarks.md`](../docs/benchmarks.md)).

Nothing here contacts an external host: the HTTP and SFTP cases start
in-process loopback servers.

## Reproduce

From the repository root, in a virtualenv with the benchmarked extras:

```bash
python -m pip install -e ".[dev,uri,http,sftp,sftp-async]"
python benchmarks/bench.py --save
```

That runs the default suite and writes
`benchmarks/results/<version>-py<major.minor>-<os>-<arch>.json`. Name another
suite after the options; its name is appended to the default file name:

```bash
python benchmarks/bench.py --help
python benchmarks/bench.py --save --samples 9 syncer
PATHLIB_NEXT_BENCH_SFTP_RECURSIVE=1 python benchmarks/bench.py --save --name my-run
```

| Option | Effect |
| --- | --- |
| `--save` | Write the run to `benchmarks/results/<name>.json`. |
| `--name NAME` | File name without `.json`. Default `<version>-py<major.minor>-<os>-<arch>[-<suite>]`. |
| `--results-dir DIR` | Write somewhere other than `benchmarks/results/`. |
| `--samples N` | Samples per metric for every case (1-100). Default: each case's own count, 1-5. |
| `--note TEXT` | Free text stored in the JSON. |

Suites: `sftp-recursive`, `sftp-recursive-copy`, `sftp-recursive-large`,
`sftp-batch`, `syncer`, `recursive-matrix`; no suite name runs the default one.
`PATHLIB_NEXT_BENCH_SFTP_RECURSIVE=1` adds the recursive SFTP copy rows to the
default suite.

## Comparing runs

Compare on the **median**: one sample's time hides run-to-run noise. Only
compare runs from the same machine and interpreter, and change one thing at a
time. The `python`, `interpreter` and `processor` fields say what a file was
measured on.

A run on a developer machine is a sanity check. Performance claims in the
changelog or release notes come from CI runs.

## Result schema (`pathlib_next.bench/1`)

One JSON object per run:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | string | `"pathlib_next.bench/1"`. |
| `name` | string | The file name without `.json`. |
| `suite` | string | `"default"` or the suite name. |
| `source` | string | `"ci"` when run under GitHub Actions, else `"local"`. |
| `note` | string or null | `--note` text. |
| `created_utc` | string | ISO 8601 UTC timestamp of the save. |
| `package` | object | `{"name": "pathlib-next", "version": <pyproject version>}`. |
| `git` | object or null | `{"commit": <HEAD sha>, "dirty": <tracked files modified>}`; null outside a git checkout. |
| `python` | string | Implementation and version, e.g. `"CPython 3.14.6"`. |
| `interpreter` | string | `<major.minor>-<os>-<arch>`, where `<os>` is `os.name` (`nt`/`posix`) or `darwin` and `<arch>` is the architecture the interpreter was built for (`sysconfig.get_platform()`). |
| `processor` | string | `<platform.system()>-<platform.machine()>` of the host. |
| `cpu_count` | integer | `os.cpu_count()`. |
| `env` | object | Benchmark environment switches (`PATHLIB_NEXT_BENCH_SFTP_RECURSIVE`). |
| `metrics` | object | Timing metrics, keyed by metric name (below). |
| `counters` | object | Call-count probes, keyed by name: `{"group": ..., <call name>: <count>, ...}`. |
| `errors` | object | Cases that raised: `{"group": ..., "error": "<Type>: <message>"}`. |
| `skipped` | object | Cases that could not run: `{"group": ..., "reason": ...}`. |

Each entry in `metrics`:

| Field | Type | Meaning |
| --- | --- | --- |
| `group` | string | `uri`, `mem`, `local`, `http`, `sftp`, `syncer` or `recursive`. |
| `min_ms` | number | Fastest sample, in milliseconds per call. |
| `median_ms` | number | Median sample, in milliseconds per call. Compare on this. |
| `max_ms` | number | Slowest sample, in milliseconds per call. |
| `samples` | integer | Samples taken (N). |
| `calls_per_sample` | integer | Calls of the operation timed in each sample; each sample's time is divided by this. |

Metric names are `<group>: <case>`, e.g. `"local: LocalPath stat() file (2k)"`
or `"sftp: asyncssh warm stat()"`. A case measured on two implementations
(`LocalPath`/`pathlib.Path`, `paramiko`/`asyncssh`) is recorded as two metrics.
A batch case (such as `read_bytes() 64-file batch`) counts the whole batch as
one call.

Excerpt (values illustrative):

```json
{
  "schema": "pathlib_next.bench/1",
  "name": "0.9.3-py3.14-posix-x86_64",
  "suite": "default",
  "source": "local",
  "python": "CPython 3.14.6",
  "interpreter": "3.14-posix-x86_64",
  "metrics": {
    "uri: path join": {
      "group": "uri",
      "min_ms": 0.0139,
      "median_ms": 0.0143,
      "max_ms": 0.0151,
      "samples": 5,
      "calls_per_sample": 10000
    }
  }
}
```

## Committed results

`results/` is tracked, so before/after numbers stay recoverable from history.

| File | What it is |
| --- | --- |
| `baseline-local-0.9.3-py3.14-nt-amd64.json` | Local sanity baseline, default suite: the working tree after 0.9.3 on a Windows developer machine, CPython 3.14 (x64 build). Not CI; not a performance claim. |
| `baseline-local-0.9.3-py3.9-nt-amd64.json` | The same run on CPython 3.9, the supported floor (x64 build). |
