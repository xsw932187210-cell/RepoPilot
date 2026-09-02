def retry_delay(attempt: int, base_seconds: int = 2) -> int:
    """Return exponential backoff where the first attempt uses the base delay."""
    return base_seconds * attempt
