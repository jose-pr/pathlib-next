import os

from pathlib_next import Path, glob
from pathlib_next.mempath import MemPath
from pathlib_next.uri import Query, Source, UriPath
from pathlib_next.utils.sync import PathSyncer

mempath = MemPath("test/test3") / "subpath"
mempath.parent.mkdir(parents=True, exist_ok=True)
mempath.write_text("test")
check = mempath.read_text()
mempath.parent.rm(recursive=True)

pass

local = Path("./_ssh")
test = list(os.scandir(local))
print(list(local.iterdir()))
query = Query({"test": "://$#!1", "test2&": [1, 2]})
q2 = Query(str(query)).to_dict()
for name, value in query:
    print(f"{name}: {value}")
src = Source(scheme="scheme", userinfo="user", host="123.com", port=0)
test = {**src}
test2 = [*src]
dest = UriPath("file:./_ssh")

#
# Norm test
#
with_dots = UriPath("a/b/c/d/../../test/.")
print(with_dots.normalized_path)

source_host = UriPath("file://test.com/path1/path2/path3/path4")
source_host.is_local()

rel_to = source_host.relative_to("/path1/path2")
dest = UriPath(dest)
test_ = UriPath("file:") / "test"
empty = UriPath()
uri = dest.as_uri()

test1 = dest / "test" / "test2/"
print(test1)

sftp_root = UriPath("sftp://root@sftpexample/")
print(sftp_root.as_posix())
authkeys = sftp_root / "root/.ssh/authorized_keys"
print(authkeys.as_posix())


def checksum(uri: UriPath):
    stat = uri.stat()
    return hash(stat.st_size)


syncer = PathSyncer(checksum, remove_missing=False)
syncer.sync((sftp_root / "root/.ssh"), dest, dry_run=True)

rocky_repo = UriPath("http://dl.rockylinux.org/pub")

glob_test = UriPath("file:./**/*.py")

for path in glob.glob(glob_test, recursive=True):
    print(path)

print(rocky_repo.is_dir())
print(list(rocky_repo.iterdir()))
