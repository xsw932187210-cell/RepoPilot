from text_utils import slugify


def test_slugify_normalizes_case_and_whitespace() -> None:
    assert slugify("  Hello   Agent WORLD  ") == "hello-agent-world"
