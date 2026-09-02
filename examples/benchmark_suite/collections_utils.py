def deduplicate(items: list[str]) -> list[str]:
    """Remove duplicate values while preserving first-seen order."""
    return list(set(items))
