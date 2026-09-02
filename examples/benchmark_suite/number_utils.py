def clamp(value: int, lower: int, upper: int) -> int:
    """Constrain value to the inclusive lower/upper interval."""
    return min(lower, max(value, upper))
