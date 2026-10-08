# `http:` and `dav:`

Both need the `http` extra.

::: pathlib_next.uri.schemes.http
    options:
      members:
        - DEFAULT_TIMEOUT
        - MAX_LISTING_BYTES
        - HttpBackend
        - HttpPath
        - HttpWriteStream
        - HttpAppendStream
      show_if_no_docstring: true
      filters: ["!^_"]

::: pathlib_next.uri.schemes.dav
    options:
      members:
        - DavPath
