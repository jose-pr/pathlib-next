# Benchmarks

`pathlib_next` includes a small benchmark harness at `benchmarks/bench.py`
for checking hot-path behavior across local, in-memory, HTTP, and SFTP
implementations.

## Run It

```bash
python benchmarks/bench.py
```

Optional stress case:

```bash
PATHLIB_NEXT_BENCH_SFTP_RECURSIVE=1 python benchmarks/bench.py
```

That opt-in flag enables the recursive SFTP probe rows, which are more
expensive and are best treated as CI/manual benchmark runs rather than
something to trust on a loaded development machine.

Narrow SFTP probes:

```bash
python benchmarks/bench.py sftp-recursive
python benchmarks/bench.py sftp-recursive-copy
python benchmarks/bench.py sftp-batch
python benchmarks/bench.py syncer
```

Use these when investigating SFTP behavior; `--help` is safe and prints
available benchmark subcommands without running benchmark work.

## What It Covers

- URI parse / compose cost
- generic path joins and name/suffix access
- `MemPath` recursive glob
- `LocalPath` vs `pathlib.Path` on common local operations
- HTTP directory traversal and parser throughput
- `paramiko` vs `asyncssh` for the same loopback SFTP workload

The SFTP comparison uses an in-process loopback server so both client
backends hit the same filesystem with the same fixture tree. That keeps the
comparison focused on backend overhead and request behavior rather than WAN
latency.

Normal `sftp://` usage defaults to the system OpenSSH client config on both
backends. The benchmark harness explicitly disables SSH config/key discovery
for its asyncssh comparison so both backends are measured with similarly
minimal connection setup instead of inheriting machine-specific SSH client
state.

## Sample Results

These numbers are from a local Windows run on July 12, 2026 with
`.venv/3.12.10`. Treat them as a shape-of-performance snapshot, not a stable
cross-machine contract.

| Benchmark Case | Time / Metric |
| --- | --- |
| URI Parse (10k) | 0.0790s |
| URI Parse, unique URIs, forced (us/parse) | 27.24us |
| URI Parse+Compose, unique URIs (us/round-trip) | 40.30us |
| Path Join (10k) | 0.1599s |
| Segments/Name Access (10k) | 0.0014s |
| Suffix/Stem Access (10k) | 0.0010s |
| Glob 1k MemPath (20) | 0.5281s |
| LocalPath vs Stdlib (2k stat) | Local: 0.0388s, Stdlib: 0.0332s |
| LocalPath construct path (10k) | Local: 0.0175s, Stdlib: 0.0184s |
| LocalPath join path (10k) | Local: 0.0498s, Stdlib: 0.0456s |
| LocalPath stat() file (2k) | Local: 0.0296s, Stdlib: 0.0328s |
| LocalPath read_bytes() 64 KiB | Local: 0.0002s, Stdlib: 0.0003s |
| LocalPath iterdir() 8 entries | Local: 0.0003s, Stdlib: 0.0003s |
| LocalPath glob('**/*.txt') 240 files | Local: 0.0143s, Stdlib: 0.0120s |
| HTTP Glob (10) | 1.0840s |
| HTTP Walk (10) | 0.4492s |
| HTTP dir listing parse, Apache `<pre>` (n=1000) | 76.4667ms/parse |
| HTTP dir listing parse, nginx `<table>` (n=1000) | 140.1685ms/parse |
| SFTP warm `stat()` | paramiko: 0.0016s, asyncssh: 0.0031s |
| SFTP `iterdir()` 72 entries | paramiko: 0.1131s, asyncssh: 0.1961s |
| SFTP `walk()` 80 files | paramiko: 0.3791s, asyncssh: 0.4584s |
| SFTP `glob('**/*.txt')` 80 files | paramiko: 0.7624s, asyncssh: 1.0949s |
| SFTP `read_bytes()` small file | paramiko: 0.0034s, asyncssh: 0.0092s |
| SFTP `read_bytes()` 64-file batch | paramiko: 0.2463s, asyncssh: 0.6104s |
| SFTP `stat()` 64-file batch | paramiko: 0.0756s, asyncssh: 0.1695s |
| SFTP `write_bytes()` 256 KiB | paramiko: 0.0140s, asyncssh: 0.0150s |
| SFTP `mkdir()` leaf dir | paramiko: 0.0018s, asyncssh: 0.0032s |
| SFTP `rename()` file | paramiko: 0.0034s, asyncssh: 0.0049s |
| SFTP `unlink()` file | paramiko: 0.0016s, asyncssh: 0.0032s |
| SFTP `copy()` single 256 KiB file | paramiko: 0.0414s, asyncssh: 0.0544s |
| SFTP `rm(recursive=True)` 9-file tree | paramiko: 0.3291s, asyncssh: TimeoutError |
| SFTP cold connect + `stat()` | paramiko: 0.0288s, asyncssh: 0.0552s |

## CI Snapshot

GitHub Actions run `Test #3` on July 12, 2026 also completed the benchmark
job successfully on `ubuntu-latest`, `windows-latest`, and `macos-latest`.
Those runner artifacts are a better cross-machine comparison than the local
developer box.

Selected CI results:

| Case | Ubuntu | Windows | macOS |
| --- | --- | --- | --- |
| URI Parse (10k) | 0.0325s | 0.0517s | 0.0582s |
| LocalPath stat() file (2k) | 0.0105s vs stdlib 0.0132s | 0.0054s vs stdlib 0.0052s | 0.0030s vs stdlib 0.0028s |
| HTTP Walk (10) | 0.0994s | 0.0949s | 0.1207s |
| SFTP `iterdir()` 72 entries | p: 0.0143s, a: 0.0157s | p: 0.0029s, a: 0.0041s | p: 0.0061s, a: 0.0074s |
| SFTP `read_bytes()` 64-file batch | p: 0.0540s, a: 0.1133s | p: 0.0735s, a: 0.1423s | p: 0.0783s, a: 0.1774s |
| SFTP `write_bytes()` 256 KiB | p: 0.0033s, a: 0.0038s | p: 0.0041s, a: 0.0049s | p: 0.3289s, a: 0.0054s |
| SFTP `copy()` single 256 KiB file | p: 0.0082s, a: 0.0095s | p: 0.0086s, a: 0.0137s | p: 0.3348s, a: 0.0154s |
| SFTP cold connect + `stat()` | p: 0.0049s, a: 0.0069s | p: 0.0126s, a: 0.0121s | p: 0.0874s, a: 0.0095s |
| SFTP `rm(recursive=True)` 9-file tree | p: 0.0546s, a: TimeoutError | p: 0.0429s, a: TimeoutError | p: 0.0494s, a: TimeoutError |

Legend: `p` = `paramiko`, `a` = `asyncssh`.

## Current Takeaways

- Post-fix note, July 12, 2026: `asyncssh` recursive remove now has a
  backend-native bounded async implementation, and generic recursive `rm()`
  now reuses listing metadata for backends such as paramiko SFTP. On a local
  Windows `.venv/3.12.10` run, `python benchmarks/bench.py sftp-recursive`
  completed the 9-file remove probe: paramiko `0.1531s`, asyncssh `0.2293s`
  (`paramiko/asyncssh=0.67x`). The older timeout rows above are pre-fix
  snapshots.
- On the same machine, `python benchmarks/bench.py sftp-batch` reported:
  64-file reads paramiko `0.1688s` vs asyncssh `0.3664s`; 64-file stats
  paramiko `0.0526s` vs asyncssh `0.1058s`; single unlink paramiko `0.0014s`
  vs asyncssh `0.0026s`.
- After native asyncssh recursive copy, `python benchmarks/bench.py
  sftp-recursive-copy` completed locally: paramiko `0.1162s`, asyncssh
  `0.1698s` for the 4-file tree. Asyncssh scaling probes reported
  `mc=1: 0.2631s` and `mc=4: 0.2921s`, so higher concurrency did not help
  this tiny loopback fixture.
- `python benchmarks/bench.py sftp-recursive-large` on a local Windows
  `.venv/3.12.10` run reported a mixed result for a 128-file tree: recursive
  copy favored asyncssh (`paramiko 13.8991s`, `asyncssh 10.9022s`), while
  recursive remove favored paramiko (`paramiko 2.8129s`, `asyncssh 4.4896s`).
  Asyncssh copy scaling was counterintuitive on loopback: `max_concurrency=1`
  was fastest at `7.6068s`, with `4` at `14.2806s` and `8` at `13.3736s`.
  Treat this as workload-specific follow-up evidence, not as a default tuning
  decision.
- `python benchmarks/bench.py syncer` on the same local run reported
  PathSyncer copy of 128 local files at `0.4524s`, dry-run at `0.0534s`, and
  remove-missing plus copy at `0.9260s`. This suggests metadata reuse may be
  worth investigating before adding parallel sync behavior.
- After PathSyncer metadata reuse, a later local `syncer` run reported copy
  at `0.4390s`, dry-run at `0.0535s`, and remove-missing plus copy at
  `0.4764s`. Treat this as a meaningful remove-missing improvement and a
  roughly neutral copy/dry-run result; local variance produced one slower
  outlier run.
- S3 recursive delete now uses provider-native `delete_objects` batching for
  prefixed trees while guarding bucket-root recursive delete. This is based
  on fake-client call-shape tests rather than live AWS timing.
- `python benchmarks/bench.py recursive-matrix` on a local Windows
  `.venv/3.12.10` verification run reported: `LocalPath` recursive copy
  `0.3115s`, `LocalPath` recursive remove `0.3363s`, `MemPath` recursive copy
  `0.0377s`, and `MemPath` recursive remove `0.0197s` for a 33-file tree.
  An earlier same-machine run was faster, so treat local filesystem timings as
  noisy and use the command primarily for trend checks. The same
  command reported the fake S3 recursive delete call shape as one
  `head_object`, one `list_objects_v2`, one `delete_objects`, and 34 deleted
  keys including the marker. GCS reported one exact-object `reload`, one
  `list_blobs`, and 34 per-blob deletes. Azure reported one exact-object
  property check, one `list_blobs`, one `delete_blobs` batch call, and no
  per-blob delete calls on the fake surface.
- `LocalPath` is competitive with `pathlib.Path` on several hot local
  operations in this run, but still trails on recursive globbing and the
  sampled `read_bytes()` case.
- Across the three CI runners, `paramiko` still wins most completed
  sync-style SFTP operations, especially directory traversal and many-small-
  file workloads.
- `asyncssh`'s recursive remove probe timed out on all three CI runners, not
  just on the local Windows machine, which makes this look like a backend or
  benchmark-shape issue rather than pure local machine noise.
- The macOS artifact showed large `paramiko` slowdowns on the single-file
  `write_bytes()` and `copy()` cases plus cold connect. That is worth a
  follow-up sanity check before treating those particular macOS numbers as a
  stable performance signal.
- On this run, every completed sync-style SFTP measurement still favored
  `paramiko`, including `iterdir()`, `glob()`, batched `read_bytes()`, and
  batched `stat()`.
- The current recursive remove probe is a useful warning sign: `paramiko`
  completed the 9-file tree removal, while `asyncssh` timed out on this
  loaded Windows machine.
- Even after aligning auth/config behavior, the loopback SFTP comparison
  still points at the same design tradeoff: `asyncssh` is going through a
  sync-to-async bridge on every small operation, while paramiko is already a
  sync client.
- The broad SFTP benchmark is useful for backend comparison even when the
  operation itself is exposed synchronously, because backend internals still
  affect overall throughput.

## Caveats

- Benchmark output is environment-sensitive: Python version, OS, filesystem,
  CPU, and installed extras all matter.
- The benchmark disables OpenSSH config/key discovery only for the loopback
  backend comparison, to avoid machine-specific SSH client state skewing the
  numbers.
- For regressions, compare before/after runs on the same machine and Python
  version rather than comparing absolute times across environments.
