# `s3:`, `gs:` and `az:`

Each needs its own extra: `s3`, `gs` or `az`.

::: pathlib_next.uri.schemes.s3
    options:
      members:
        - BaseS3Backend
        - S3Backend
        - S3Path
      show_if_no_docstring: true
      filters: ["!^_"]

::: pathlib_next.uri.schemes.gs
    options:
      members:
        - BaseGsBackend
        - GsBackend
        - GsPath
      show_if_no_docstring: true
      filters: ["!^_"]

::: pathlib_next.uri.schemes.az
    options:
      members:
        - BaseAzBackend
        - AzBackend
        - AzPath
      show_if_no_docstring: true
      filters: ["!^_"]
