def page_offset(page: int, page_size: int) -> int:
    """Return the zero-based database offset for a one-based page number."""
    return (page - 1) * page_size + 1
