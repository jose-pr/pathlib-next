from uripath.uri import Uri
from uripath.schemes import *
from uripath.sync import UriSyncer

dest = Uri('file:./_ssh')
dest = Uri(dest)
empty = Uri()
uri = dest.as_uri()

print(list(dest.iterdir()))
test1 = dest / 'test' / 'test2/'
print(test1)

sftp_root = Uri('sftp://root@sftpexample/')
authkeys = sftp_root / 'root/.ssh/authorized_keys'

def checksum(uri:Uri):
    stat = uri.stat()
    return hash(stat.st_size)
syncer =UriSyncer(checksum, remove_missing=False)
syncer.sync((sftp_root / 'root/.ssh'), dest, dry_run=True)

rocky_repo = Uri('http://dl.rockylinux.org/pub')

print(rocky_repo.is_dir())
print(list(rocky_repo.iterdir()))
