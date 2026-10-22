from tinyledger.totals import net_total


def test_empty():
    assert net_total([]) == 0
