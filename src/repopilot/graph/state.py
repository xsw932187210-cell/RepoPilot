from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class RepoPilotState(TypedDict, total=False):
    task_id: str
    repository_url: str
    issue_title: str
    issue_body: str
    base_branch: str
    test_command: str
    max_iterations: int
    workspace: str

    plan: dict[str, Any]
    research_tree: list[str]
    research_files: dict[str, str]
    test_strategy: str
    reviewer_feedback: list[str]
    edits: list[dict[str, Any]]
    changed_files: list[str]
    diff: str
    test_result: dict[str, Any]
    review: dict[str, Any]
    iteration: int
    node_metrics: Annotated[list[dict[str, Any]], operator.add]

    human_approved: bool
    human_feedback: str
    pull_request_url: str | None
    status: str
    error: str
