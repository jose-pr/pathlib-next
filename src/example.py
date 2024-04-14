from uripath.uri import Uri
from uripath.schemes import *

dest = Uri('file:./_keys')
uri = dest.as_uri()

sftp_root = Uri('sftp://root@sftpexample/')

print(list(sftp_root.iterdir()))

authkeys = sftp_root / 'root/.ssh/authorized_keys'

authkeys.copy(dest, overwrite=True)
print((sftp_root / 'root/.ssh/authorized_keys').read_text())
