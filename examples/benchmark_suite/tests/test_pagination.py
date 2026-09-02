from pagination import page_offset


def test_page_offset_is_zero_based() -> None:
    assert page_offset(1, 20) == 0
    assert page_offset(3, 20) == 40
