def cache_key(tenant_id: str, user_id: str) -> str:
    """Create a cache key scoped to one tenant."""
    return user_id
