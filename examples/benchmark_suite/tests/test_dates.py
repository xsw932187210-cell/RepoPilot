from datetime import date

from dates import inclusive_days


def test_inclusive_days_counts_both_endpoints() -> None:
    assert inclusive_days(date(2026, 9, 1), date(2026, 9, 1)) == 1
    assert inclusive_days(date(2026, 9, 1), date(2026, 9, 3)) == 3
