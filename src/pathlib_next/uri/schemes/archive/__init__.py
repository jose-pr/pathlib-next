from ._base import ArchiveUri as ArchiveUri
from ._base import _split_archive_path as _split_archive_path
from .tar import TarUri as TarUri
from .zip import ZipUri as ZipUri


class ArchiveZipUri(ZipUri):
    """`archive+zip:` explicit-format scheme: same as `zip:`, registered
    under a second scheme name so `archive+zip:` always wins over
    `archive:`'s auto-detection regardless of the outer archive's
    extension/content.

    Named without a leading underscore on purpose: `UriPath._schemes()`
    looks up `__SCHEMES` via `getattr(cls, f"_{cls.__name__}__SCHEMES")`,
    which assumes `cls.__name__` has no leading underscore of its own --
    Python's real name-mangling strips a leading underscore from the class
    name before mangling, so a class named e.g. `_ArchiveZipUri` mangles
    `__SCHEMES` to `_ArchiveZipUri__SCHEMES` while this lookup instead
    computes `__ArchiveZipUri__SCHEMES`, silently finding nothing (caught
    by `_schemes()`'s bare `except AttributeError: return ()`) and falling
    back to the generic `UriPath` stub. Every `__SCHEMES`-registered class
    in this codebase must have a name with no leading underscore."""

    __SCHEMES = ("archive+zip",)
    __slots__ = ()


class ArchiveTarUri(TarUri):
    """`archive+tar:` explicit-format scheme: same as `tar:`, registered
    under a second scheme name so `archive+tar:` always wins over
    `archive:`'s auto-detection. See `ArchiveZipUri` for why this can't be
    named with a leading underscore."""

    __SCHEMES = ("archive+tar",)
    __slots__ = ()
