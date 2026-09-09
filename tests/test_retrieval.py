from pathlib import Path

from repopilot.config import Settings
from repopilot.repository import RepositoryContext, WorkspaceManager
from repopilot.retrieval import HybridCodeRetriever, tokenize


def test_tokenize_splits_paths_snake_case_and_camel_case() -> None:
    assert tokenize("src/payment_service/PaymentProcessor.py") == (
        "src",
        "payment",
        "service",
        "payment",
        "processor",
        "py",
    )


def test_hybrid_retrieval_combines_symbols_and_dependency_neighbors() -> None:
    files = {
        "src/billing/service.py": (
            "class PaymentProcessor:\n"
            "    def refund_payment(self, payment_id: str) -> bool:\n"
            "        return False\n"
        ),
        "src/email/service.py": (
            "class EmailSender:\n    def send_message(self, value: str) -> None:\n        pass\n"
        ),
        "tests/test_billing.py": (
            "from src.billing.service import PaymentProcessor\n\n"
            "def test_refund_payment():\n"
            "    assert PaymentProcessor().refund_payment('p-1')\n"
        ),
        "README.md": "A service repository with billing and email modules.\n",
    }
    result = HybridCodeRetriever(max_files=3, max_context_chars=20_000).retrieve(
        files,
        issue_text="PaymentProcessor refund_payment incorrectly returns false",
        search_terms=["refund payment"],
    )

    assert result.strategy == "hybrid-bm25-symbol-v2"
    assert result.hits[0].path == "src/billing/service.py"
    assert "PaymentProcessor" in result.hits[0].matched_symbols
    billing_test = next(hit for hit in result.hits if hit.path == "tests/test_billing.py")
    assert billing_test.dependency_score > 0
    assert "src/billing/service.py" in billing_test.related_paths


def test_retrieval_budget_skips_oversized_files_without_truncating_selected_content() -> None:
    small_content = "def calculate_total(items):\n    return sum(items)\n"
    result = HybridCodeRetriever(max_files=3, max_context_chars=10_000).retrieve(
        {
            "src/oversized.py": "calculate_total " * 900,
            "src/totals.py": small_content,
            "tests/test_totals.py": (
                "from src.totals import calculate_total\n\n"
                "def test_total():\n"
                "    assert calculate_total([1, 2]) == 3\n"
            ),
        },
        issue_text="calculate_total returns the wrong total",
        search_terms=["calculate_total"],
    )

    assert result.skipped_for_budget == 1
    assert {hit.path for hit in result.hits} == {
        "src/totals.py",
        "tests/test_totals.py",
    }


def test_repository_context_exposes_evidence_and_respects_render_budget(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "src" / "orders.py"
    test = workspace / "tests" / "test_orders.py"
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    source.write_text(
        "class OrderService:\n"
        "    def submit_order(self, order_id: str) -> str:\n"
        "        return order_id\n",
        encoding="utf-8",
    )
    test.write_text(
        "from src.orders import OrderService\n\n"
        "def test_submit_order():\n"
        "    assert OrderService().submit_order('o-1') == 'o-1'\n",
        encoding="utf-8",
    )
    context = WorkspaceManager(Settings(max_context_files=2, max_context_chars=10_000)).inspect(
        workspace,
        "OrderService submit_order should preserve the id",
        ["submit order"],
    )

    rendered = context.render()
    assert context.strategy == "hybrid-bm25-symbol-v2"
    assert context.candidate_count == 2
    assert len(context.files) == 2
    assert context.editable_paths == tuple(context.files)
    assert len(rendered) <= 10_000
    assert "Retrieval evidence:" in rendered
    assert source.read_text(encoding="utf-8") in rendered
    assert test.read_text(encoding="utf-8") in rendered
    assert all("score" in item for item in context.evidence)


def test_invalid_python_is_still_retrievable() -> None:
    result = HybridCodeRetriever(max_files=1, max_context_chars=10_000).retrieve(
        {"broken_parser.py": "def parse_payload(:\n    return payload\n"},
        issue_text="parse_payload syntax error",
        search_terms=["parser"],
    )
    assert [hit.path for hit in result.hits] == ["broken_parser.py"]


def test_zero_score_files_do_not_fill_context_when_relevant_files_exist() -> None:
    result = HybridCodeRetriever(max_files=5, max_context_chars=10_000).retrieve(
        {
            "src/orders.py": "def submit_order():\n    return 'submitted'\n",
            "src/unrelated.py": "def archive_record():\n    return None\n",
            "README.md": "An intentionally generic project description.\n",
        },
        issue_text="submit_order fails",
        search_terms=["submit order"],
    )
    assert [hit.path for hit in result.hits] == ["src/orders.py"]


def test_no_match_fallback_is_bounded_and_deterministic() -> None:
    files = {f"{letter}.py": "value = 1\n" for letter in "edcba"}
    retriever = HybridCodeRetriever(max_files=5, max_context_chars=10_000)
    result = retriever.retrieve(files, issue_text="unknownword", search_terms=[])
    assert [hit.path for hit in result.hits] == ["a.py", "b.py", "c.py"]
    assert all(hit.score == 0 for hit in result.hits)
    assert retriever.retrieve({}, issue_text="unknownword", search_terms=[]).hits == ()


def test_render_never_truncates_file_bodies_under_a_smaller_budget() -> None:
    short_content = "def small():\n    return 1\n"
    context = RepositoryContext(
        tree=["large.py", "small.py"],
        files={"large.py": "large content " * 1_000, "small.py": short_content},
    )
    rendered = context.render(max_chars=1_000)
    assert len(rendered) <= 1_000
    assert "--- large.py ---" not in rendered
    assert "large content" not in rendered
    assert short_content in rendered
    assert context.render(max_chars=0) == ""


def test_deeply_nested_python_falls_back_to_text_retrieval() -> None:
    content = "result = " + "+" * 5_000 + "1\n"
    result = HybridCodeRetriever(max_files=1, max_context_chars=10_000).retrieve(
        {"deep_expression.py": content},
        issue_text="deep_expression error",
        search_terms=[],
    )
    assert [hit.path for hit in result.hits] == ["deep_expression.py"]


def test_exact_rule_identifier_outweighs_generic_content_matches() -> None:
    files = {
        "rules/no_command.py": "def match(command):\n    return False\n",
        **{
            f"rules/generic_{index}.py": "command not found installed executable " * 20
            for index in range(15)
        },
    }
    result = HybridCodeRetriever(max_files=3, max_context_chars=10_000).retrieve(
        files,
        issue_text="The no_command rule should ignore not-found text for installed commands",
        search_terms=[],
    )

    assert result.hits[0].path == "rules/no_command.py"
    assert result.hits[0].path_score >= 8
