from retry import retry_delay


def test_retry_delay_uses_exponential_backoff() -> None:
    assert [retry_delay(attempt) for attempt in range(3)] == [2, 4, 8]
