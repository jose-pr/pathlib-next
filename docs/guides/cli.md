# `uripath` CLI

`pathlib_next` installs a `uripath` command for basic operations on local
paths and URI-backed paths. It is also runnable as
`python -m pathlib_next.tools.uripath`.

```bash
uripath read s3://bucket/path/file.txt
uripath write output.txt "hello"
uripath cp input.txt sftp://host/tmp/input.txt
uripath rm --recursive sftp://host/tmp/work
uripath sync --remove-missing source/ target/
```

Use `-` for stdin or stdout wherever a command reads or writes bytes:

```bash
uripath read path.txt > copy.txt
cat notes.txt | uripath write path.txt
uripath cp - s3://bucket/stdin.bin < local.bin
uripath cp s3://bucket/stdout.bin - > local.bin
```

## Commands

| Command | Behavior |
| --- | --- |
| `read PATH` | Writes `PATH`'s bytes to stdout, in chunks. `PATH` may be `-` (stdin). |
| `write PATH [DATA] [--encoding ENC]` | Writes `DATA` encoded with `ENC` (default `utf-8`), or stdin's bytes when `DATA` is omitted. Replaces an existing file. |
| `rm PATH [-r/--recursive] [--missing-ok] [--ignore-error]` | Removes a file or an empty directory; `-r` removes a tree. `--missing-ok` accepts a missing path; `--ignore-error` skips failures during the removal. |
| `cp SOURCE TARGET [-r/--recursive] [--overwrite] [--no-follow-symlinks] [--no-preserve-metadata]` | Copies a file, or a tree with `-r`. An existing target is refused without `--overwrite`, also when either side is `-` (which cannot be combined with `-r`). `--no-follow-symlinks` copies a link as a link. |
| `sync SOURCE TARGET [--dry-run] [--remove-missing] [--size-only] [-v/--verbose] [--no-follow-symlinks]` | One-way sync with `PathSyncer`, comparing file content (a backend-native digest or a streamed md5), so same-size edits are copied. `--size-only` compares sizes only and misses same-size edits. `--remove-missing` deletes target entries that are not in the source. `--dry-run` prints each planned change without changing anything; `-v` prints each change a real run makes. `--no-follow-symlinks` recreates source symlinks instead of following them. |

`sync --dry-run` and `sync -v` print one line per change:

```text
would mkdir dst
would copy src/a.txt -> dst/a.txt
would remove dst/old.txt
```

The other actions are `replace` (a target entry of another type is replaced)
and `symlink`.

## Paths

An argument containing `://`, or starting with a scheme that a class
registers (`data:`, `zip:`, `file:`, ...), is a `UriPath`; anything else is a
local path, so `C:/data`, `12:30.txt` and `notes:draft` stay local.
Without the `uri` extra, local paths and `-` still work and a URI argument
reports the extra to install.

Errors are printed as `uripath: <ExceptionType>: <message>` on stderr with
exit status 1. A closed stdout (`uripath read big.bin | head`) exits with 141
and Ctrl-C with 130.
