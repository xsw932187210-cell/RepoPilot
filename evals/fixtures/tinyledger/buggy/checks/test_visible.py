from tinyledger.totals import net_total


def test_positive_amounts():
    assert net_total([2, 3]) == 5


def test_empty():
    assert net_total([]) == 0
