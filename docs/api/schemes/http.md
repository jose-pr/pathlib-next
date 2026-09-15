# `http:` and `dav:`

Both need the `http` extra.

::: pathlib_next.uri.schemes.http
    options:
      members:
        - DEFAULT_TIMEOUT
        - HttpBackend
        - HttpPath
      show_if_no_docstring: true
      filters: ["!^_"]

::: pathlib_next.uri.schemes.dav
    options:
      members:
        - DavPath
