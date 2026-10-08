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


def test_a_plus_in_a_mapping_is_escaped_so_it_cannot_read_as_a_space():
    query = Query({"sig": "ab+cd=="})
    assert str(query) == "sig=ab%2Bcd%3D%3D"
    assert query.decode() == [("sig", "ab+cd==")]
    offset = Query({"since": "2026-10-07T00:00:00+00:00"})
    assert "+" not in str(offset)
    assert offset.to_dict() == {"since": ["2026-10-07T00:00:00+00:00"]}
    assert str(Query({"a+b": "c"})) == "a%2Bb=c"
    assert str(Query([("a+b", "c+d")])) == "a%2Bb=c%2Bd"


def test_what_the_encoder_wrote_decodes_to_what_it_was_given():
    values = [
        "+",
        "=",
        "&",
        "#",
        "%",
        " ",
        ";",
        "a+b c",
        "100%+1",
        "\u00fc+\u4e2d",
        "?/:@",
    ]
    for value in values:
        for query in (Query({"k": value}), Query([("k", value)])):
            assert query.decode() == [("k", value)], value
            assert Query(str(query)).decode() == [("k", value)], value
    name = Query({"a=b+c&d": "x"})
    assert name.decode() == [("a=b+c&d", "x")]


def test_a_query_built_from_text_is_kept_byte_for_byte():
    for text in ("a+b=c+d", "a=1&b=%2B&c=%zz", "x=a b", "sig=ab%2Bcd%3D%3D"):
        assert str(Query(text)) == text
    assert Query("a+b=c+d").decode() == [("a+b", "c+d")]
    assert Query("sig=ab%2Bcd%3D%3D").decode() == [("sig", "ab+cd==")]


def test_decode_reads_a_plus_as_a_plus_not_as_a_space():
    # RFC 3986, not form-urlencoded: only %XX escapes are decoded.
    assert Query("q=a+b&r=a%20b").decode() == [("q", "a+b"), ("r", "a b")]


def test_a_signature_set_with_with_query_reaches_the_wire_escaped():
    from pathlib_next.uri import Uri

    uri = Uri("http://h/x").with_query({"sig": "ab+cd=="})
    assert uri.as_uri() == "http://h/x?sig=ab%2Bcd%3D%3D"
    assert uri.query.to_dict() == {"sig": ["ab+cd=="]}
