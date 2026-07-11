# pathlib_next

Generic Path Protocol based pathlib implementation for URI paths with file access support for sftp, http, file schemes.

For the full feature list, installation extras, quick-start examples for every
integration, and known limitations, see the
[project README](https://github.com/jose-pr/pathlib_next#readme). This site adds the
generated [API reference](api/reference.md) and the [changelog](changelog.md).

## Installation

```bash
pip install pathlib_next
```

## Quick start

```python
from pathlib_next import Path
from pathlib_next.uri import UriPath

# Use the unified path interface
local_path = Path("./my_folder")
http_path = UriPath("http://example.com/data.txt")

# Read and print text if it exists
if http_path.exists():
    print(http_path.read_text())
```
