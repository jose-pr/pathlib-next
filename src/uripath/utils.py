import functools as _functools
import typing as _ty
import time as _time
from email.utils import parsedate as _parsedate
import typing as _ty


def parsedate(date: _ty.Union[str, _time.struct_time, tuple, float]):
    if date is None:
        return _time.time()
    if isinstance(date, str):
        date = _parsedate(date)
    return _time.mktime(date)


def sizeof_fmt(num: _ty.Union[int, float]):
    for unit in ("", "K", "M", "G", "T", "P", "E", "Z"):
        if abs(num) < 1024:
            if unit:
                return "%3.1f%s" % (num, unit)
            else:
                return int(num)
        num /= 1024.0
    return "%.1f%s" % (num, "Y")


def notimplemented(method):
    @_functools.wraps(method)
    def _notimplemented(method):
        raise NotImplementedError(f"Not implement method  {method.__name__}")

    return _notimplemented
