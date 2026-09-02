from collections_utils import deduplicate


def test_deduplicate_preserves_first_seen_order() -> None:
    assert deduplicate(["planner", "coder", "planner", "reviewer"]) == [
        "planner",
        "coder",
        "reviewer",
    ]
