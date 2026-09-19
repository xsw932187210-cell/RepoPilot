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
    research_editable_paths: list[str] | None
    research_evidence: list[dict[str, Any]]
    retrieval_query_terms: list[str]
    retrieval_strategy: str
    retrieval_candidate_count: int
    retrieval_selected_chars: int
    retrieval_skipped_for_budget: int
    capability_policy_version: str
    initial_retrieval: dict[str, Any]
    test_strategy: str
    reviewer_feedback: list[str]
    edits: list[dict[str, Any]]
    changed_files: list[str]
    diff: str
    test_result: dict[str, Any]
    review: dict[str, Any]
    iteration: int
    policy_retry_count: int
    node_metrics: Annotated[list[dict[str, Any]], operator.add]

    human_approved: bool
    human_feedback: str
    pull_request_url: str | None
    status: str
    error: str
