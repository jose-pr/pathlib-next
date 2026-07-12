from .archive import TarUri as TarUri
from .archive import ZipUri as ZipUri
from .data import DataUri as DataUri
from .file import FileUri as FileUri
from .ftp import FtpPath as FtpPath

try:
    from .http import HttpPath as HttpPath
except ImportError:
    pass
try:
    from .dav import DavPath as DavPath
except ImportError:
    pass
try:
    from .sftp import SftpPath as SftpPath
except ImportError:
    pass
try:
    from .s3 import S3Path as S3Path
except ImportError:
    pass
try:
    from .github import GitHubPath as GitHubPath
except ImportError:
    pass
try:
    from .gitlab import GitLabPath as GitLabPath
except ImportError:
    pass
try:
    from .git import GitPath as GitPath
except ImportError:
    pass
