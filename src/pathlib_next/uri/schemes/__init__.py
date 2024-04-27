from .file import FileUri

try:
    from .http import HttpPath
except ImportError:
    pass
try:
    from .sftp import SftpPath
except ImportError:
    pass
