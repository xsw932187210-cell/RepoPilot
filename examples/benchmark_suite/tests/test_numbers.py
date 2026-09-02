from number_utils import clamp


def test_clamp_enforces_both_bounds() -> None:
    assert clamp(-3, 0, 10) == 0
    assert clamp(5, 0, 10) == 5
    assert clamp(14, 0, 10) == 10
