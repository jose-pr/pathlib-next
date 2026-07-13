# `uripath` CLI

`pathlib_next` installs a `uripath` command for basic path operations across
local paths and URI-backed paths.

```powershell
uripath read s3://bucket/path/file.txt
uripath write output.txt "hello"
uripath cp input.txt sftp://host/tmp/input.txt
uripath rm --recursive mem://workspace/tmp
uripath sync --remove-missing source/ target/
```

Use `-` for stdin or stdout where a command reads or writes bytes:

```powershell
uripath read path.txt
uripath write path.txt
uripath cp - s3://bucket/stdin.bin
uripath cp s3://bucket/stdout.bin -
```

Commands:

| Command | Behavior |
| --- | --- |
| `read PATH` | Writes `PATH` bytes to stdout. `PATH=-` copies stdin to stdout. |
| `write PATH [DATA]` | Writes `DATA` as text, or stdin bytes when `DATA` is omitted. |
| `rm PATH` | Removes a file or empty directory. Add `--recursive`, `--missing-ok`, or `--ignore-error` as needed. |
| `cp SOURCE TARGET` | Copies bytes or paths. Supports `--recursive`, `--overwrite`, `--no-follow-symlinks`, and `--no-preserve-metadata`. |
| `sync SOURCE TARGET` | Uses `PathSyncer` with a size checksum. Supports `--dry-run`, `--remove-missing`, and `--no-follow-symlinks`. |

Plain filesystem paths use `LocalPath`; URI-looking paths use `UriPath`
scheme dispatch.
