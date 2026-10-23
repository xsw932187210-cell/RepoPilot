"""withheld-fixture-sentinel-72c891: evaluator-only assertions."""

from tinyledger.totals import net_total


def test_empty():
    assert net_total([]) == 0


def test_refunds_are_subtracted():
    assert net_total([2, -5]) == -3


def test_only_refunds():
    assert net_total([-2, -3]) == -5
