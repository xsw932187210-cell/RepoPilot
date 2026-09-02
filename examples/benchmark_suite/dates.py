from datetime import date


def inclusive_days(start: date, end: date) -> int:
    """Count calendar days including both start and end."""
    return (end - start).days
