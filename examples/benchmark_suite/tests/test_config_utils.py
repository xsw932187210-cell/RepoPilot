from config_utils import parse_bool


def test_parse_bool_handles_false_strings() -> None:
    assert parse_bool("true") is True
    assert parse_bool("YES") is True
    assert parse_bool("false") is False
    assert parse_bool("0") is False
