try:
    from .uri import Uri as Uri
    from .uri import UriPath as UriPath
    from .uri.schemes import *
except ImportError:
    pass
from .fspath import *
from .path import *
from .utils import glob as glob
from .utils import sync as sync
