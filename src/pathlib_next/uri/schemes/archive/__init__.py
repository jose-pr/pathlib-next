from __future__ import annotations

from ._base import ArchiveUri as ArchiveUri
from ._base import _split_archive_path as _split_archive_path
from .tar import TarUri as TarUri
from .zip import ZipUri as ZipUri

# `archive+zip:` and `archive+tar:` are second scheme names of `ZipUri` and
# `TarUri` (they pin the format, which the extension-detecting `archive:`
# does not); these class names stay importable.
ArchiveZipUri = ZipUri
ArchiveTarUri = TarUri
