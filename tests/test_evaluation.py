from pathlib import Path

import pytest

from repopilot.evaluation import run


@pytest.mark.asyncio
async def test_ten_case_mock_regression_baseline() -> None:
    project_root = Path(__file__).parents[1]
    report = await run(project_root / "evals" / "cases.jsonl", provider="mock")
    assert report["summary"]["count"] == 10
    assert report["summary"]["successful"] == 10
    assert report["summary"]["success_rate"] == 1.0
    assert report["summary"]["scope_match_rate"] == 1.0
    assert report["metadata"]["provider"] == "mock"
    assert report["metadata"]["model"] == "deterministic-mock-v1"
