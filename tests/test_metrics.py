from datetime import UTC, datetime, timedelta

from repopilot.api import summarize_task_metrics
from repopilot.models import TaskStatus, TaskView


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
        result={
            "node_metrics": [
                {"node": "planner", "duration_ms": 20, "iteration": 0},
                {"node": "test_runner", "duration_ms": 80, "iteration": 1},
                {"node": "reviewer", "duration_ms": 10, "iteration": 1},
                {"node": "test_runner", "duration_ms": 60, "iteration": 2},
            ]
        },
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
