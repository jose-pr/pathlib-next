from pathlib_next.uri import Uri, Query
from pathlib_next.uri.schemes import *
from pathlib_next.sync import PathSyncer
from pathlib_next import glob

query = Query({'test':'://$#!1', 'test2&': [1,2]})
q2 =  Query(str(query)).to_dict()
dest = Uri('file:./_ssh')
dest = Uri(dest)
test_ = Uri('file:') / 'test'
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
syncer =PathSyncer(checksum, remove_missing=False)
syncer.sync((sftp_root / 'root/.ssh'), dest, dry_run=True)

rocky_repo = Uri('http://dl.rockylinux.org/pub')

glob_test = Uri("file:./**/*.py")

for path in glob.iglob(glob_test, recursive=True):
    print(path)

print(rocky_repo.is_dir())
print(list(rocky_repo.iterdir()))
