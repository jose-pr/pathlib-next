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

That opt-in flag enables the recursive SFTP copy comparison, which is more
expensive than the default suite.

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

## Sample Results

These numbers are from a local Windows run on July 12, 2026 with
`.venv/3.12.10`. Treat them as a shape-of-performance snapshot, not a stable
cross-machine contract.

| Benchmark Case | Time / Metric |
| --- | --- |
| URI Parse (10k) | 0.6778s |
| URI Parse, unique URIs, forced (us/parse) | 261.19us |
| URI Parse+Compose, unique URIs (us/round-trip) | 276.16us |
| Path Join (10k) | 0.4769s |
| Segments/Name Access (10k) | 0.0064s |
| Suffix/Stem Access (10k) | 0.0028s |
| Glob 1k MemPath (20) | 2.5886s |
| LocalPath vs Stdlib (2k stat) | Local: 0.2195s, Stdlib: 0.2524s |
| LocalPath construct path (10k) | Local: 0.1600s, Stdlib: 0.0970s |
| LocalPath join path (10k) | Local: 0.1940s, Stdlib: 0.1580s |
| LocalPath stat() file (2k) | Local: 0.0899s, Stdlib: 0.0766s |
| LocalPath read_bytes() 64 KiB | Local: 0.0003s, Stdlib: 0.0002s |
| LocalPath iterdir() 8 entries | Local: 0.0002s, Stdlib: 0.0002s |
| LocalPath glob('**/*.txt') 240 files | Local: 0.0316s, Stdlib: 0.0200s |
| HTTP Glob (10) | 1.3912s |
| HTTP Walk (10) | 0.5828s |
| HTTP dir listing parse, Apache `<pre>` (n=1000) | 65.7116ms/parse |
| HTTP dir listing parse, nginx `<table>` (n=1000) | 266.5333ms/parse |
| SFTP warm `stat()` | paramiko: 0.0020s, asyncssh: 0.0031s |
| SFTP `iterdir()` 72 entries | paramiko: 0.1162s, asyncssh: 0.1272s |
| SFTP `walk()` 80 files | paramiko: 0.3769s, asyncssh: 0.4886s |
| SFTP `glob('**/*.txt')` 80 files | paramiko: 2.1086s, asyncssh: 2.1841s |
| SFTP `read_bytes()` small file | paramiko: 0.0050s, asyncssh: 0.0104s |
| SFTP `write_bytes()` 256 KiB | paramiko: 0.0201s, asyncssh: 0.0169s |
| SFTP `mkdir()` leaf dir | paramiko: 0.0029s, asyncssh: 0.0040s |
| SFTP `rename()` file | paramiko: 0.0042s, asyncssh: 0.0054s |
| SFTP `unlink()` file | paramiko: 0.0021s, asyncssh: 0.0038s |
| SFTP `copy()` single 256 KiB file | paramiko: 0.0583s, asyncssh: 0.0757s |
| SFTP cold connect + `stat()` | paramiko: 0.0402s, asyncssh: 0.0532s |

## Current Takeaways

- `LocalPath` is close to `pathlib.Path` on basic file I/O, but still loses
  on path construction, joins, and recursive globbing in this run.
- The loopback SFTP comparison currently favors `paramiko` for most small
  sync-style operations, with `asyncssh` only pulling ahead on the sampled
  256 KiB write case in this run.
- The broad SFTP benchmark is useful for backend comparison even when the
  operation itself is exposed synchronously, because backend internals still
  affect overall throughput.

## Caveats

- Benchmark output is environment-sensitive: Python version, OS, filesystem,
  CPU, and installed extras all matter.
- The loopback SFTP harness currently finishes with some noisy asyncssh
  teardown warnings even though the measurements complete successfully.
- For regressions, compare before/after runs on the same machine and Python
  version rather than comparing absolute times across environments.
