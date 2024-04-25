import typing as _ty
import uritools as _uritools


class Query(str):
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
        separator=SEPARATOR
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
                query: str = _uritools._querydict(query, separator, encoding).decode()
            else:
                query = _uritools._querylist(query, separator, encoding).decode()

        obj = str.__new__(cls, query)
        obj._encoding = encoding
        obj._separator = separator
        return obj

    def decode(query) -> list[tuple[str, str | None]]:
        return _uritools.SplitResultString("", "", "", str(query), "").getquerylist(
            query._separator, query._encoding
        )

    def to_dict(query, *, single=False):
        query_: dict[str, list[str | None]] = {}
        for k, v in query.decode():
            if single:
                query_[k] = v
            else:
                query_.setdefault(k, []).append(v)
        return query_
