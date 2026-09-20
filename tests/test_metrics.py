from datetime import UTC, datetime, timedelta

from repopilot.api import summarize_task_metrics
from repopilot.models import TaskStatus, TaskView
from repopilot.worker import result_summary


def test_task_summary_preserves_retrieval_evidence_without_source_bodies() -> None:
    initial = {
        "strategy": "hybrid-bm25-symbol-v1",
        "selected_files": ["src/service.py"],
        "selected_chars": 100,
        "evidence": [{"path": "src/service.py", "score": 9.5}],
    }
    summary = result_summary(
        {
            "initial_retrieval": initial,
            "research_files": {"src/service.py": "private source body"},
            "research_evidence": [{"path": "src/service.py", "score": 10.5}],
            "retrieval_selected_chars": 200,
        }
    )
    assert summary["initial_retrieval"] == initial
    assert summary["retrieval"]["selected_files"] == ["src/service.py"]
    assert summary["retrieval"]["selected_chars"] == 200
    assert "private source body" not in str(summary)


def test_task_metrics_aggregate_retries_and_sandbox_time() -> None:
    created = datetime.now(UTC)
    task = TaskView(
        id="task-1",
        status=TaskStatus.COMPLETED,
        repository_url="demo://buggy-calculator",
        issue_title="Metric aggregation",
        issue_body="Aggregate repeated graph node timings.",
        base_branch="main",
        test_command="python -m pytest -q",
        max_iterations=2,
        graph_thread_id="task-task-1",
        state_version=4,
        result={
            "retrieval": {
                "strategy": "hybrid-bm25-symbol-v1",
                "candidate_count": 42,
                "selected_files": ["src/service.py", "tests/test_service.py"],
                "selected_chars": 8_000,
            },
            "node_metrics": [
                {"node": "planner", "duration_ms": 20, "iteration": 0},
                {"node": "test_runner", "duration_ms": 80, "iteration": 1},
                {"node": "reviewer", "duration_ms": 10, "iteration": 1},
                {"node": "test_runner", "duration_ms": 60, "iteration": 2},
            ]
        },
        result_schema_version=1,
        created_at=created,
        updated_at=created + timedelta(milliseconds=250),
    )
    metrics = summarize_task_metrics(task)
    assert metrics.wall_time_ms == 250
    assert metrics.node_time_ms == 170
    assert metrics.node_runs["test_runner"] == 2
    assert metrics.node_duration_ms["test_runner"] == 140
    assert metrics.sandbox_time_ms == 140
    assert metrics.iterations == 2
    assert metrics.retrieval_strategy == "hybrid-bm25-symbol-v1"
    assert metrics.retrieval_candidate_files == 42
    assert metrics.retrieval_selected_files == 2
    assert metrics.retrieval_context_chars == 8_000
