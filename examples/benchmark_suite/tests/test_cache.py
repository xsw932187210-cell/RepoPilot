from cache import cache_key


def test_cache_key_is_tenant_scoped() -> None:
    assert cache_key("tenant-a", "user-7") == "tenant-a:user-7"
    assert cache_key("tenant-b", "user-7") != cache_key("tenant-a", "user-7")
