def slugify(value: str) -> str:
    """Create a lowercase, hyphen-separated slug."""
    return value.strip().replace(" ", "_")
