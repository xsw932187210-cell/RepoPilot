"""Deterministic routing from public issue text and first visible retrieval only."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from repopilot.repository import RepositoryContext

ROUTING_VERSION = "visible-rules-v1"
STRATEGIES = ("oneshot", "workflow", "auto")
WORKFLOW_MIN_CALLS = 3


@dataclass(frozen=True)
class RoutingPolicy:
    version: str = ROUTING_VERSION
    max_editable_files: int = 2
    max_context_chars: int = 20_000
    min_issue_chars: int = 40
    max_issue_chars: int = 1_500

    @classmethod
    def from_settings(cls, settings):
        return cls(
            max_editable_files=settings.routing_max_editable_files,
            max_context_chars=settings.routing_max_context_chars,
            min_issue_chars=settings.routing_min_issue_chars,
            max_issue_chars=settings.routing_max_issue_chars,
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def decide_route(
    requested: str, issue_title: str, issue_body: str, context: RepositoryContext,
    remaining_calls: int, policy: RoutingPolicy,
) -> dict:
    if requested not in STRATEGIES:
        raise ValueError("unsupported_execution_strategy")
    text = f"{issue_title}\n{issue_body}".strip()
    editable = len(context.editable_paths or ())
    ambiguous = bool(re.search(
        r"\b(refactor|redesign|architecture|unclear|intermittent)\b|重构|架构|不清楚|偶尔",
        text, re.IGNORECASE,
    ))
    features = {
        "issue_chars": len(text), "editable_files": editable,
        "context_chars": context.selected_chars, "ambiguous_scope": ambiguous,
        "retrieval_hits": sum(
            bool(item.get("matched_terms") or item.get("matched_symbols"))
            for item in context.evidence
        ),
    }
    reasons = []
    if requested != "auto":
        selected = requested
        reasons.append("explicit_strategy")
    else:
        if not policy.min_issue_chars <= len(text) <= policy.max_issue_chars:
            reasons.append("issue_scope_outside_simple_bounds")
        if ambiguous:
            reasons.append("ambiguous_or_broad_issue")
        if editable > policy.max_editable_files:
            reasons.append("multiple_editable_files")
        if context.selected_chars > policy.max_context_chars:
            reasons.append("large_visible_context")
        selected = "workflow" if reasons else "oneshot"
        if not reasons:
            reasons.append("small_bounded_visible_task")
    # Legacy workflow requests keep their existing generation/error behavior.
    # New lightweight routes stop deterministically before an unfounded request.
    if requested != "workflow" and (not editable or not features["retrieval_hits"]):
        selected, reasons = "stop", ["no_editable_retrieval_hit"]
    minimum = WORKFLOW_MIN_CALLS if selected == "workflow" else 1
    if requested != "workflow" and selected != "stop" and remaining_calls < minimum:
        selected, reasons = "stop", ["insufficient_model_budget"]
    return {
        "policy_version": policy.version, "policy_fingerprint": policy.fingerprint,
        "requested": requested, "selected": selected, "reasons": reasons,
        "features": features, "remaining_calls": remaining_calls, "upgrades": 0,
    }
