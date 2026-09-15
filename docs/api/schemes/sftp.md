# `sftp:`

Needs the `sftp` extra (paramiko backend) or the `sftp-async` extra (asyncssh
backend).

::: pathlib_next.uri.schemes.sftp
    options:
      members:
        - BaseSftpBackend
        - SftpPath

::: pathlib_next.uri.schemes.sftp._paramiko.SftpBackend

::: pathlib_next.uri.schemes.sftp._asyncssh.AsyncsshSftpBackend
