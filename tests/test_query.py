from pathlib_next.uri.query import Query


def test_from_string_roundtrip():
    q = Query("a=1&b=2")
    assert q.to_dict() == {"a": ["1"], "b": ["2"]}


def test_from_dict_single_values():
    q = Query({"a": "1", "b": "2"})
    assert set(str(q).split("&")) == {"a=1", "b=2"}


def test_from_dict_list_values_repeats_key():
    # B25 regression: this is the example.py snippet exercised by
    # test_smoke.py; reimplemented against public uriencode() (not
    # uritools' private _querydict/_querylist).
    q = Query({"test": "://$#!1", "test2&": [1, 2]})
    d = q.to_dict()
    assert d["test"] == ["://$#!1"]
    assert d["test2&"] == ["1", "2"]


def test_iteration_yields_pairs():
    q = Query({"a": "1", "b": ["2", "3"]})
    pairs = sorted(q)
    assert pairs == [("a", "1"), ("b", "2"), ("b", "3")]


def test_special_characters_encoded_and_decoded():
    q = Query({"key": "a b/c?d#e&f"})
    # round-trip through the encoded string form
    decoded = Query(str(q)).to_dict()
    assert decoded["key"] == ["a b/c?d#e&f"]


def test_none_value_emits_bare_key():
    q = Query([("flag", None)])
    assert str(q) == "flag"


def test_custom_separator():
    q = Query({"a": "1", "b": "2"}, separator=";")
    assert ";" in str(q)
    assert "&" not in str(q)
