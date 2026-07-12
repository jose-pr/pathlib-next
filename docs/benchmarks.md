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

## Current Takeaways

- `LocalPath` is competitive with `pathlib.Path` on several hot local
  operations in this run, but still trails on recursive globbing and the
  sampled `read_bytes()` case.
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
