from __future__ import annotations

import collections.abc as _abc
import typing as _ty
import uritools as _uritools

# Same reserved/safe set uritools' own (private, version-fragile) query
# helpers use -- reimplemented here against the *public* uriencode() so this
# doesn't break across uritools versions (B25).
_SAFE_QUERY = "!$&'()*+,;=:@/?"


def _querylist(
    items: _ty.Sequence[_ty.Tuple[str, _ty.Any]], sep: str, encoding: str
) -> bytes:
    safe = _SAFE_QUERY.replace(sep, "")
    terms = []
    for key, value in items:
        name = _uritools.uriencode(key, safe, encoding)
        if value is None:
            terms.append(name)
        elif isinstance(value, (bytes, str)):
            terms.append(name + b"=" + _uritools.uriencode(value, safe, encoding))
        else:
            terms.append(name + b"=" + _uritools.uriencode(str(value), safe, encoding))
    return sep.encode("ascii").join(terms)


def _querydict(
    mapping: _ty.Mapping[str, _ty.Any], sep: str, encoding: str
) -> bytes:
    items = []
    for key, value in mapping.items():
        if isinstance(value, (bytes, str)):
            items.append((key, value))
        elif isinstance(value, _abc.Iterable):
            items.extend((key, v) for v in value)
        else:
            items.append((key, value))
    return _querylist(items, sep, encoding)


class Query(str):
    """A URI query string (`str` subclass) that can also be built from a
    dict/list of pairs and decoded back with `to_dict()`/iteration."""

    __slots__ = ("_encoding", "_separator")
    SEPARATOR = "&"
    ENCODING = "utf-8"

    def __new__(
        cls,
        query: (
            str
            | _ty.Sequence[tuple[str, str | None]]
            | _ty.Mapping[str, str | None | _ty.Sequence[str | None]]
        ),
        *,
        encoding=ENCODING,
        separator=SEPARATOR,
    ):
        if isinstance(query, Query):
            _encoding = query._encoding
            _separator = query._separator
        else:
            _encoding = None
            _separator = None

        encoding = encoding or _encoding or cls.ENCODING
        separator = separator or _separator or cls.SEPARATOR
        if isinstance(query, str):
            pass
        else:
            if isinstance(query, _ty.Mapping):
                query: str = _querydict(query, separator, encoding).decode()
            else:
                query = _querylist(query, separator, encoding).decode()

        obj = str.__new__(cls, query)
        obj._encoding = encoding
        obj._separator = separator
        return obj

    def decode(query) -> list[tuple[str, str | None]]:
        return _uritools.SplitResultString("", "", "", str(query), "").getquerylist(
            query._separator, query._encoding
        )

    def __iter__(self):
        return iter(self.decode())

    def to_dict(query, *, single=False):
        query_: dict[str, list[str | None]] = {}
        for k, v in query.decode():
            if single:
                query_[k] = v
            else:
                query_.setdefault(k, []).append(v)
        return query_
