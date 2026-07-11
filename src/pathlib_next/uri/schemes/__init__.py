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
    from .sftp import SftpPath as SftpPath
except ImportError:
    pass
