import json
from pathlib import Path

import pytest

from repopilot.config import Settings
from repopilot.evaluation import evaluate_case, retrieval_metrics, run


def test_retrieval_metrics_measure_partial_recall_and_deduplicate_ranks() -> None:
    metrics = retrieval_metrics(
        {"a.py", "b.py", "c.py"},
        ["noise.py", "a.py", "a.py", "other.py", "b.py"],
    )
    assert metrics["retrieval_target_hit"] is False
    assert metrics["retrieval_target_recall"] == pytest.approx(2 / 3)
    assert metrics["retrieval_recall_at_3"] == pytest.approx(1 / 3)
    assert metrics["retrieval_recall_at_5"] == pytest.approx(2 / 3)
    assert metrics["retrieval_first_relevant_rank"] == 2
    assert metrics["retrieval_reciprocal_rank"] == 0.5


def test_retrieval_metrics_handle_misses_and_reject_missing_ground_truth() -> None:
    metrics = retrieval_metrics({"target.py"}, [])
    assert metrics["retrieval_target_hit"] is False
    assert metrics["retrieval_target_recall"] == 0.0
    assert metrics["retrieval_recall_at_3"] == 0.0
    assert metrics["retrieval_recall_at_5"] == 0.0
    assert metrics["retrieval_first_relevant_rank"] is None
    assert metrics["retrieval_reciprocal_rank"] == 0.0
    with pytest.raises(ValueError, match="non-empty expected_files"):
        retrieval_metrics(set(), ["a.py"])


@pytest.mark.asyncio
async def test_evaluation_uses_configured_retrieval_limits() -> None:
    project_root = Path(__file__).parents[1]
    case = json.loads(
        (project_root / "evals" / "cases.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    report = await evaluate_case(
        case,
        project_root / "examples" / "benchmark_suite",
        Settings(
            model_provider="mock",
            max_context_files=1,
            max_context_chars=10_000,
            max_file_bytes=1_000,
        ),
    )
    assert report["success"] is True
    assert len(report["retrieved_files"]) == 1
    assert report["retrieval_selected_chars"] <= 10_000
    assert all(item["content_chars"] <= 1_000 for item in report["retrieval_evidence"])


@pytest.mark.asyncio
async def test_legacy_local_evaluator_refuses_model_generated_code() -> None:
    with pytest.raises(ValueError, match="real-task Docker"):
        await evaluate_case(
            {"name": "unsafe-local-real"},
            Path("unused"),
            Settings(
                _env_file=None,
                model_provider="openai",
                openai_api_key="fixture-not-a-secret",  # noqa: S106 - non-secret fixture
            ),
        )


@pytest.mark.asyncio
async def test_ten_case_mock_regression_baseline() -> None:
    project_root = Path(__file__).parents[1]
    report = await run(project_root / "evals" / "cases.jsonl", provider="mock")
    assert report["summary"]["count"] == 10
    assert report["summary"]["successful"] == 10
    assert report["summary"]["success_rate"] == 1.0
    assert report["summary"]["scope_match_rate"] == 1.0
    assert report["summary"]["retrieval_target_recall"] == 1.0
    assert report["summary"]["retrieval_all_targets_hit_rate"] == 1.0
    assert report["summary"]["retrieval_recall_at_3"] == 1.0
    assert report["summary"]["retrieval_recall_at_5"] == 1.0
    assert report["summary"]["retrieval_mrr"] == 1.0
    assert report["summary"]["mean_retrieved_files"] <= 12
    assert all(case["retrieval_strategy"] == "hybrid-bm25-symbol-v2" for case in report["cases"])
    assert report["metadata"]["provider"] == "mock"
    assert report["metadata"]["model"] == "deterministic-mock-v1"
    assert report["metadata"]["retrieval_phase"] == "first_pass_before_edits"
    assert report["metadata"]["retrieval_config"]["max_context_files"] == 12
