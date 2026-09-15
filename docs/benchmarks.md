# Benchmarks

`pathlib_next` includes a benchmark harness at `benchmarks/bench.py` for
checking hot-path behavior across local, in-memory, HTTP, and SFTP
implementations. It needs the extras the benchmarked schemes use:
`pip install -e ".[dev,uri,http,sftp,sftp-async]"`.

## Run It

```bash
python benchmarks/bench.py            # default suite
python benchmarks/bench.py --help     # options and suites; runs nothing
```

Narrower suites:

```bash
python benchmarks/bench.py sftp-recursive
python benchmarks/bench.py sftp-recursive-copy
python benchmarks/bench.py sftp-recursive-large
python benchmarks/bench.py sftp-batch
python benchmarks/bench.py syncer
python benchmarks/bench.py recursive-matrix
```

Optional stress case for the default suite:

```bash
PATHLIB_NEXT_BENCH_SFTP_RECURSIVE=1 python benchmarks/bench.py
```

That flag enables the recursive SFTP copy rows, which are more expensive and
are best treated as manual benchmark runs rather than something to trust on a
loaded development machine.

## Saved Results

Every metric is timed over several samples. `--save` writes the run as JSON to
`benchmarks/results/<name>.json`, with the minimum, median and maximum
milliseconds per call of each metric plus the interpreter, platform and git
commit it ran on:

```bash
python benchmarks/bench.py --save                 # default suite
python benchmarks/bench.py --save --samples 9 syncer
```

Options go before the suite name. `--samples N` applies one sample count to
every case; without it each case uses its own (1-5). The result schema and the
committed baselines are described in
[`benchmarks/README.md`](https://github.com/jose-pr/pathlib-next/blob/main/benchmarks/README.md).

Compare two runs on their medians, and only between runs from the same machine
and interpreter. A run on a developer machine is a sanity check; performance
claims in release notes come from CI runs.

## What It Covers

- URI parse / compose cost
- generic path joins and name/suffix access
- `MemPath` recursive glob
- `LocalPath` vs `pathlib.Path` on common local operations
- HTTP directory traversal and parser throughput
- `paramiko` vs `asyncssh` for the same loopback SFTP workload
- `PathSyncer` on local trees, and recursive copy/remove call shapes for the
  object-store backends (with fake clients)

The SFTP comparison uses an in-process loopback server so both client
backends hit the same filesystem with the same fixture tree. That keeps the
comparison focused on backend overhead and request behavior rather than WAN
latency.

Normal `sftp://` usage defaults to the system OpenSSH client config on both
backends. The benchmark harness explicitly disables SSH config/key discovery
for its asyncssh comparison so both backends are measured with similarly
minimal connection setup instead of inheriting machine-specific SSH client
state.

## Past Snapshots

The tables below are dated snapshots from July 2026, before results were
saved as JSON. Each row is a single run (the harness took no repeated samples
then), so treat them as a shape of performance, not a stable contract.

### Local run, July 12, 2026

A local Windows run with Python 3.12.

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
| SFTP `rm(recursive=True)` 9-file tree | paramiko: 0.3291s, asyncssh: TimeoutError (see below) |
| SFTP cold connect + `stat()` | paramiko: 0.0288s, asyncssh: 0.0552s |

### CI run, July 12, 2026

GitHub Actions run `Test #3` completed the benchmark job on `ubuntu-latest`,
`windows-latest`, and `macos-latest` with Python 3.12.

| Case | Ubuntu | Windows | macOS |
| --- | --- | --- | --- |
| URI Parse (10k) | 0.0582s | 0.0325s | 0.0517s |
| LocalPath stat() file (2k) | 0.0030s vs stdlib 0.0028s | 0.0105s vs stdlib 0.0132s | 0.0054s vs stdlib 0.0052s |
| HTTP Walk (10) | 0.1207s | 0.0994s | 0.0949s |
| SFTP `iterdir()` 72 entries | p: 0.0061s, a: 0.0074s | p: 0.0143s, a: 0.0157s | p: 0.0029s, a: 0.0041s |
| SFTP `read_bytes()` 64-file batch | p: 0.0783s, a: 0.1774s | p: 0.0540s, a: 0.1133s | p: 0.0735s, a: 0.1423s |
| SFTP `write_bytes()` 256 KiB | p: 0.3289s, a: 0.0054s | p: 0.0033s, a: 0.0038s | p: 0.0041s, a: 0.0049s |
| SFTP `copy()` single 256 KiB file | p: 0.3348s, a: 0.0154s | p: 0.0082s, a: 0.0095s | p: 0.0086s, a: 0.0137s |
| SFTP cold connect + `stat()` | p: 0.0874s, a: 0.0095s | p: 0.0049s, a: 0.0069s | p: 0.0126s, a: 0.0121s |
| SFTP `rm(recursive=True)` 9-file tree | p: 0.0494s, a: TimeoutError | p: 0.0546s, a: TimeoutError | p: 0.0429s, a: TimeoutError |

Legend: `p` = `paramiko`, `a` = `asyncssh`.

Until September 2026 this page showed the three columns rotated (Windows data
under Ubuntu, macOS data under Windows, Ubuntu data under macOS). The columns
above follow the runners' own artifacts. The Windows column is confirmed by
its artifact's Windows line endings; the Ubuntu and macOS columns follow the
artifact names, which the local-filesystem timings agree with (the Linux
runner has the fastest `stat()`).

## Findings From Past Runs

- **`asyncssh` recursive remove timed out on every runner** in the July 12
  snapshots, locally and in CI. A later fix gave `asyncssh` a backend-native
  bounded async recursive remove, and generic recursive `rm()` now reuses
  listing metadata for backends such as paramiko SFTP. A local Windows
  Python 3.12 run of `python benchmarks/bench.py sftp-recursive` then
  completed the 9-file remove probe: paramiko `0.1531s`, asyncssh `0.2293s`
  (`paramiko/asyncssh=0.67x`).
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
  Python 3.12 run reported a mixed result for a 128-file tree: recursive
  copy favored asyncssh (`paramiko 13.8991s`, `asyncssh 10.9022s`), while
  recursive remove favored paramiko (`paramiko 2.8129s`, `asyncssh 4.4896s`).
  Asyncssh copy scaling was counterintuitive on loopback: `max_concurrency=1`
  was fastest at `7.6068s`, with `4` at `14.2806s` and `8` at `13.3736s`.
- **0.8.3: `AsyncsshSftpBackend` default `max_concurrency` raised 8 → 16.** A
  cleaner 128-file loopback sweep of `mc ∈ {1,2,4,8,16}` (median of 3,
  Python 3.14) showed recursive copy improving *monotonically* with
  concurrency (mc=1 `1.66s` → mc=8 `1.47s` ≈ 1.13x → mc=16 `1.42s`, a further
  ≈3%), and recursive remove flat within noise (median spread
  `0.498`..`0.551`, mc=16 marginally best). This supersedes the earlier
  "mc=1 fastest" loopback reading, which came from an older code path. 16
  stays within asyncssh's SFTP request window. **Loopback only**: there is no
  per-operation latency, and a high-latency remote link may favour higher
  concurrency still, so 16 is a safe modest default, not a tuned optimum.
  Override per backend via `AsyncsshSftpBackend(max_concurrency=…)`.
- `python benchmarks/bench.py syncer` on a local run reported PathSyncer copy
  of 128 local files at `0.4524s`, dry-run at `0.0534s`, and remove-missing
  plus copy at `0.9260s`. After PathSyncer started reusing listing metadata,
  a later run reported copy at `0.4390s`, dry-run at `0.0535s`, and
  remove-missing plus copy at `0.4764s`: a clear remove-missing improvement
  and a roughly neutral copy/dry-run result.
- S3 recursive delete uses provider-native `delete_objects` batching for
  prefixed trees while guarding bucket-root recursive delete. This is based
  on fake-client call-shape tests rather than live AWS timing.
- `python benchmarks/bench.py recursive-matrix` on a local Windows Python 3.12
  run reported: `LocalPath` recursive copy `0.3115s`, `LocalPath` recursive
  remove `0.3363s`, `MemPath` recursive copy `0.0377s`, and `MemPath`
  recursive remove `0.0197s` for a 33-file tree. Local filesystem timings are
  noisy; use the command primarily for trend checks. The same command
  reported the fake S3 recursive delete call shape as one `head_object`, one
  `list_objects_v2`, one `delete_objects`, and 34 deleted keys including the
  marker. GCS reported one exact-object `reload`, one `list_blobs`, and 34
  per-blob deletes. Azure reported one exact-object property check, one
  `list_blobs`, one `delete_blobs` batch call, and no per-blob delete calls
  on the fake surface.
- `LocalPath` was competitive with `pathlib.Path` on several hot local
  operations in these runs, but trailed on recursive globbing and the sampled
  `read_bytes()` case.
- Across the three CI runners, `paramiko` won most completed sync-style SFTP
  operations, especially directory traversal and many-small-file workloads.
- **The Ubuntu CI run showed large `paramiko` slowdowns** on single-file
  `write_bytes()` (`0.3289s` vs asyncssh `0.0054s`) and `copy()` (`0.3348s`),
  and on cold connect (`0.0874s`). The Windows and macOS runners did not.
  Re-check those cases on Linux before treating them as a stable signal.
- The loopback SFTP comparison points at a design tradeoff even with
  auth/config behavior aligned: `asyncssh` goes through a sync-to-async
  bridge on every small operation, while paramiko is already a sync client.
  Backend internals affect throughput even when the operation itself is
  exposed synchronously.

### Not measured by wall clock

- **Native checksum protocol (0.9.0).** The saving is structural: when
  `PathSyncer`'s default policy can use both sides' native digest (e.g.
  `SftpPath` against a server implementing the filexfer draft's
  `check-file-handle` extension; OpenSSH does not), a file comparison
  transfers **zero content bytes** for a match-or-mismatch verdict, versus
  the streaming fallback's full read on *both* sides (`2 * file_size` for an
  unchanged file that still needs comparing). The project's SFTP test server
  (asyncssh's `SFTPServer`) has no checksum extension, so a live run against
  it only exercises the streaming fallback; genuine native-path timing needs
  a server that implements the extension. The wire-level fake tests in
  `tests/test_sftp.py` (`test_paramiko_checksum_*`) are the correctness proof.
- **`PathSyncer(quick_check=True)` (0.9.0).** Also structural. For an
  unchanged non-local file with matching `st_size`/`st_mtime`, the pre-check
  skips the checksum step (native or streaming) entirely: **zero content
  bytes and zero extension round trips**, versus the native-checksum path's
  one request per file. It only applies when metadata already agrees; any
  mismatch (including an unchanged file whose mtime a prior copy did not
  preserve) still pays for a real checksum.

## Caveats

- Benchmark output is environment-sensitive: Python version, OS, filesystem,
  CPU, and installed extras all matter.
- The benchmark disables OpenSSH config/key discovery only for the loopback
  backend comparison, to avoid machine-specific SSH client state skewing the
  numbers.
- For regressions, compare before/after runs on the same machine and Python
  version rather than comparing absolute times across environments.
