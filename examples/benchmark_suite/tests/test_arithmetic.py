from arithmetic import add, subtract


def test_add_and_subtract_have_distinct_behavior() -> None:
    assert add(7, 4) == 11
    assert subtract(7, 4) == 3
