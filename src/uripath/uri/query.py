import typing as _ty
import uritools as _uritools

class Query(str):
    SEPARATOR = "&"
    ENCODING = "utf-8"

    def __new__(
        cls,
        query: (
            str
            | _ty.Sequence[tuple[str, str | None]]
            | _ty.Mapping[str, str | None | _ty.Sequence[str | None]]
        ),
    ):
        if isinstance(query, str):
            pass
        else:
            if isinstance(query, _ty.Mapping):
                query: str = _uritools._querydict(
                    query, cls.SEPARATOR, cls.ENCODING
                ).decode()
            else:
                query = _uritools._querylist(query, cls.SEPARATOR, cls.ENCODING).decode()

        return str.__new__(cls, query)

    def decode(query) -> list[tuple[str, str | None]]:
        return _uritools.SplitResultString("", "", "", str(query), "").getquerylist(
            query.SEPARATOR, query.ENCODING
        )

    def to_dict(query):
        query_: dict[str, list[str | None]] = {}
        for k, v in query.decode():
            query_.setdefault(k, []).append(v)
        return query_
