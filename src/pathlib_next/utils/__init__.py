import collections
import functools as _functools
import operator as _operator
import time as _time
import typing as _ty
from email.utils import parsedate as _parsedate
from threading import RLock

try:
    ParamSpec = _ty.ParamSpec  # 3.10+
except AttributeError:
    try:
        from typing_extensions import ParamSpec
    except ImportError:

        class ParamSpec(_ty.TypeVar, _root=True):
            """Minimal `ParamSpec` stand-in for 3.9 without `typing_extensions`.

            Only the `.args` / `.kwargs` attributes are needed: they appear in
            annotations that must merely *evaluate*, and a plain `TypeVar` has
            neither. `typing_extensions` is not a runtime dependency, so the
            fallback keeps a bare 3.9 install importable.
            """

            @property
            def args(self):
                return self

            @property
            def kwargs(self):
                return self


K = ParamSpec("K")
V = _ty.TypeVar("V")


class LRU(_ty.Generic[K, V]):
    """Thread-safe memoizing LRU cache over a function, callable like the
    function itself; `invalidate(*args)` evicts and recomputes an entry."""

    def __init__(self, func: _ty.Callable[K, V], maxsize=128):
        self.cache = collections.OrderedDict()
        self.func = func
        self._maxsize = maxsize
        self.lock = RLock()

    @property
    def maxsize(self):
        return self._maxsize

    @maxsize.setter
    def maxsize(self, maxsize: int):
        cache = self.cache
        with self.lock:
            self._maxsize = maxsize
            while len(cache) > maxsize:
                cache.popitem(last=False)

    def __call__(self, *args: K.args) -> V:
        cache = self.cache
        with self.lock:
            if args in cache:
                cache.move_to_end(args)
                return cache[args]
        result = self.func(*args)
        with self.lock:
            cache[args] = result
            if len(cache) > self._maxsize:
                cache.popitem(last=False)
        return result

    def invalidate(self, *args: K.args) -> V:
        with self.lock:
            if args in self.cache:
                self.cache.pop(args, None)

        return self(*args)


def parsedate(date: _ty.Union[str, _time.struct_time, tuple, float]):
    # Missing/unparseable dates yield epoch 0, not "now" -- a caller with no
    # Last-Modified header shouldn't have that read as "just modified" and
    # poison checksum/sync freshness comparisons.
    if date is None:
        return 0
    if isinstance(date, str):
        date = _parsedate(date)
        if date is None:
            return 0
    return _time.mktime(date)


def sizeof_fmt(num: _ty.Union[int, float]) -> str:
    for unit in ("", "K", "M", "G", "T", "P", "E", "Z"):
        if abs(num) < 1024:
            if unit:
                return "%3.1f%s" % (num, unit)
            else:
                return str(int(num))
        num /= 1024.0
    return "%.1f%s" % (num, "Y")


def notimplemented(method):
    @_functools.wraps(method)
    def _notimplemented(*args, **kwargs):
        raise NotImplementedError(f"Method not implemented: {method.__name__}")

    return _notimplemented


def as_error_handler(
    ignore_error: _ty.Union[bool, _ty.Callable[..., bool], None],
    *,
    default: bool = False,
) -> _ty.Callable[..., bool]:
    """Normalize an `ignore_error` argument into a *callable* error policy.

    Every `ignore_error` parameter in this library accepts either a bool or
    a callable, but the callables have **deliberately different arities**
    per call site (`Path.rm()` -> `(error, path)`, `Path.copy()` ->
    `(error)`, `PathSyncer.sync()` -> `(error, source, target, event)`).
    Unifying those arities would break existing callers, so this helper only
    normalizes the *bool* case and passes a supplied callable through
    untouched -- it is invoked with whatever arguments its own call site
    already uses.

    `None` means "no policy supplied": it resolves to `default` (False for
    every current caller, i.e. raise on the first error), which preserves
    `Path.copy(ignore_error=None)`'s documented meaning.

    Centralizing this keeps a fourth call site from drifting back into
    calling a bool (see `PathSyncer.sync()`'s symlink branch, which did
    exactly that and raised `TypeError: 'bool' object is not callable`).
    """
    if callable(ignore_error):
        return ignore_error
    if ignore_error is None:
        ignore_error = default
    result = bool(ignore_error)
    return lambda *args, **kwargs: result


def as_mode(mode: _ty.Union[int, str]) -> int:
    """Normalize a permission `mode` to an int, parsing `str` as **octal**.

    `chmod("0755")` is the spelling everyone actually writes a mode in --
    `chmod(1)`, Ansible, Dockerfiles, every shell script -- and stdlib
    refuses it (`TypeError: 'str' object cannot be interpreted as an
    integer`). This library accepts it, which makes the base explicit and
    non-negotiable rather than leaving it to each call site.

    **Why base 8 is mandatory here, and never a plain `int()`:** `"0755"`
    parsed as decimal is 755, which is `0o1363` -- a different *and valid*
    mode. Nothing would raise; the file would just end up with permissions
    nobody intended. That is exactly why stdlib declines strings, so the
    only safe way to accept them is to parse them one way, in one place.

    Accepts an optional `0o`/`0O` prefix. Anything outside `[0-7]` raises
    `ValueError` rather than being coerced -- a mode is not a number that
    happens to be written in octal, it is octal.

    An `int` passes through untouched (including `0o755`, which *is* an
    int by the time it gets here -- the literal is resolved by the parser,
    so `chmod(0o755)` and `chmod("0755")` agree).
    """
    if isinstance(mode, str):
        text = mode.strip()
        if text[:2].lower() == "0o":
            text = text[2:]
        if not text or any(character not in "01234567" for character in text):
            raise ValueError(f"invalid octal mode: {mode!r}")
        return int(text, 8)
    return _operator.index(mode)


#: Canonical "leave this ownership field unchanged" sentinel for `chown()`.
#: `None` is the API-level spelling; `-1` is accepted too because that is
#: `os.chown`'s own sentinel and callers coming from it reach for it.
UNCHANGED = None


def as_owner(
    uid: _ty.Union[int, str, None], gid: _ty.Union[int, str, None]
) -> "_ty.Tuple[_ty.Optional[int], _ty.Optional[int]]":
    """Normalize a `chown()` uid/gid pair to canonical `int | None`.

    `None` means "leave unchanged". `-1` is accepted as an alias for it,
    since that is how `os.chown` spells the same thing and callers arriving
    from the stdlib reach for it out of habit.

    The point of centralizing this is that **every backend spells
    "unchanged" differently** -- `os.chown` wants `-1`, SFTP `setstat` wants
    the field omitted from the attrs entirely, and other middlewares want
    `None`. If each scheme translated the caller's input itself, that is
    three chances for the semantics to disagree. Normalizing once on `Path`
    means a backend's `_chown()` receives an already-canonical pair and only
    has to convert to its own wire spelling.

    A `str` is passed through as a *name* (`shutil.chown` accepts user and
    group names, and it is useful not to force a caller to resolve them) --
    backends that cannot resolve names should say so rather than guess.
    """

    def _one(value):
        if value is None or isinstance(value, str):
            return value
        value = _operator.index(value)
        return None if value == -1 else value

    return _one(uid), _one(gid)


from .checksum import md5 as md5, sha256 as sha256
from .archive import make_archive as make_archive, unpack_archive as unpack_archive
