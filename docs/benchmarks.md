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
| URI Parse (10k) | 0.0906s |
| URI Parse, unique URIs, forced (us/parse) | 28.26us |
| URI Parse+Compose, unique URIs (us/round-trip) | 43.87us |
| Path Join (10k) | 0.1837s |
| Segments/Name Access (10k) | 0.0015s |
| Suffix/Stem Access (10k) | 0.0017s |
| Glob 1k MemPath (20) | 0.5064s |
| LocalPath vs Stdlib (2k stat) | Local: 0.0354s, Stdlib: 0.0375s |
| LocalPath construct path (10k) | Local: 0.0128s, Stdlib: 0.0249s |
| LocalPath join path (10k) | Local: 0.0667s, Stdlib: 0.0694s |
| LocalPath stat() file (2k) | Local: 0.0458s, Stdlib: 0.0495s |
| LocalPath read_bytes() 64 KiB | Local: 0.0005s, Stdlib: 0.0002s |
| LocalPath iterdir() 8 entries | Local: 0.0001s, Stdlib: 0.0001s |
| LocalPath glob('**/*.txt') 240 files | Local: 0.0254s, Stdlib: 0.0147s |
| HTTP Glob (10) | 1.2651s |
| HTTP Walk (10) | 0.5654s |
| HTTP dir listing parse, Apache `<pre>` (n=1000) | 76.2783ms/parse |
| HTTP dir listing parse, nginx `<table>` (n=1000) | 135.5039ms/parse |
| SFTP warm `stat()` | paramiko: 0.0012s, asyncssh: 0.0026s |
| SFTP `iterdir()` 72 entries | paramiko: 0.1362s, asyncssh: 0.1424s |
| SFTP `walk()` 80 files | paramiko: 0.4285s, asyncssh: 0.5982s |
| SFTP `glob('**/*.txt')` 80 files | paramiko: 0.9762s, asyncssh: 1.2515s |
| SFTP `read_bytes()` small file | paramiko: 0.0041s, asyncssh: 0.0087s |
| SFTP `write_bytes()` 256 KiB | paramiko: 0.0155s, asyncssh: 0.0159s |
| SFTP `mkdir()` leaf dir | paramiko: 0.0023s, asyncssh: 0.0030s |
| SFTP `rename()` file | paramiko: 0.0031s, asyncssh: 0.0045s |
| SFTP `unlink()` file | paramiko: 0.0016s, asyncssh: 0.0029s |
| SFTP `copy()` single 256 KiB file | paramiko: 0.0345s, asyncssh: 0.0385s |
| SFTP cold connect + `stat()` | paramiko: 0.0171s, asyncssh: 0.0286s |

## Current Takeaways

- `LocalPath` is competitive with `pathlib.Path` on several hot local
  operations in this run, but still trails on recursive globbing and the
  sampled `read_bytes()` case.
- Even after aligning auth/config behavior, the loopback SFTP comparison
  still tends to favor `paramiko` for small sync-style calls. That matches
  the design tradeoff: `asyncssh` is going through a sync-to-async bridge on
  every small operation, while paramiko is already a sync client.
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
