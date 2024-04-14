import functools as _functools
import typing as _ty
import time as _time
from email.utils import parsedate as _parsedate

from htmllistparse import parse as _htmlparse
import bs4 as _bs4
import typing as _ty

if _ty.TYPE_CHECKING:
    import requests


class FileEntry(_ty.NamedTuple):
    name: str
    modified: _ty.Optional[_time.struct_time]
    size: _ty.Optional[int]
    description: _ty.Optional[str]


def ls(url, session: "requests.Session", **requests_args) -> list[FileEntry]:
    req = session.get(url, **requests_args)
    req.raise_for_status()
    soup = _bs4.BeautifulSoup(req.content, "html5lib")
    _, listing = _htmlparse(soup)
    return listing


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
