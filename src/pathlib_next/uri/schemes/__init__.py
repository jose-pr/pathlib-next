from .file import FileUri as FileUri

try:
    from .http import HttpPath as HttpPath
except ImportError:
    pass
try:
    from .sftp import SftpPath as SftpPath
except ImportError:
    pass
