from identity import normalize_email


def test_normalize_email_is_case_insensitive() -> None:
    assert normalize_email("  Agent.User@Example.COM ") == "agent.user@example.com"
