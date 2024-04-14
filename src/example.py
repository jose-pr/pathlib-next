from uripath.uri import PureUriPath, UriPath
from uripath.http import HttpPath
from uripath.sftp import SftpPath
from uripath.file import LocalPath

from pathlib import PurePosixPath, Path

dest = UriPath('file:./keys')

sftp_root = UriPath('sftp://root@sftpexample/')

print(list(sftp_root.iterdir()))

authkeys = sftp_root / 'root/.ssh/authorized_keys'

authkeys.copy(dest, overwrite=True)
print((sftp_root / 'root/.ssh/authorized_keys').read_text())
