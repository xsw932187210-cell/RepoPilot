from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from langchain_openai import ChatOpenAI

from repopilot.config import Settings
from repopilot.eval_runtime import ModelCallBudget
from repopilot.models import CodeChangeOutput, FileEdit, PlanOutput, ReviewOutput, SandboxResult
from repopilot.repository import RepositoryContext

_StructuredOutput = TypeVar("_StructuredOutput", PlanOutput, CodeChangeOutput, ReviewOutput)


@dataclass(frozen=True, slots=True)
class DeterministicFix:
    selectors: tuple[str, ...]
    path: str
    old: str
    new: str
    reason: str


_DETERMINISTIC_FIXES = (
    DeterministicFix(
        selectors=("add(a, b)", "add returning", "subtracts b"),
        path="calculator.py",
        old=(
            "def add(a: int, b: int) -> int:\n"
            '    """Return the sum of two integers."""\n'
            "    return a - b"
        ),
        new=(
            "def add(a: int, b: int) -> int:\n"
            '    """Return the sum of two integers."""\n'
            "    return a + b"
        ),
        reason="Correct addition so it no longer subtracts the second operand.",
    ),
    DeterministicFix(
        selectors=("add(a, b)", "add returning", "subtracts b"),
        path="arithmetic.py",
        old=(
            "def add(a: int, b: int) -> int:\n"
            '    """Return the sum of two integers."""\n'
            "    return a - b"
        ),
        new=(
            "def add(a: int, b: int) -> int:\n"
            '    """Return the sum of two integers."""\n'
            "    return a + b"
        ),
        reason="Correct addition so it no longer subtracts the second operand.",
    ),
    DeterministicFix(
        selectors=("slugify",),
        path="text_utils.py",
        old='return value.strip().replace(" ", "_")',
        new='return "-".join(value.strip().lower().split())',
        reason="Normalize case and collapse whitespace into URL-safe hyphen separators.",
    ),
    DeterministicFix(
        selectors=("clamp",),
        path="number_utils.py",
        old="return min(lower, max(value, upper))",
        new="return max(lower, min(value, upper))",
        reason="Apply the lower and upper bounds in the correct order.",
    ),
    DeterministicFix(
        selectors=("pagination offset", "page offset"),
        path="pagination.py",
        old="return (page - 1) * page_size + 1",
        new="return (page - 1) * page_size",
        reason="Use a zero-based database offset without the extra element.",
    ),
    DeterministicFix(
        selectors=("deduplicate", "first-seen order"),
        path="collections_utils.py",
        old="return list(set(items))",
        new="return list(dict.fromkeys(items))",
        reason="Remove duplicates while preserving deterministic first-seen order.",
    ),
    DeterministicFix(
        selectors=("normalize_email", "email normalization"),
        path="identity.py",
        old="return email.strip()",
        new="return email.strip().lower()",
        reason="Canonicalize email casing after trimming surrounding whitespace.",
    ),
    DeterministicFix(
        selectors=("retry backoff", "exponential backoff"),
        path="retry.py",
        old="return base_seconds * attempt",
        new="return base_seconds * (2**attempt)",
        reason="Calculate exponential rather than linear retry delay.",
    ),
    DeterministicFix(
        selectors=("inclusive_days", "inclusive day"),
        path="dates.py",
        old="return (end - start).days",
        new="return (end - start).days + 1",
        reason="Count both range endpoints in the inclusive duration.",
    ),
    DeterministicFix(
        selectors=("parse_bool", "boolean parser"),
        path="config_utils.py",
        old="return bool(value)",
        new='return value.strip().lower() in {"1", "true", "yes", "on"}',
        reason="Parse recognized truthy strings instead of using string truthiness.",
    ),
    DeterministicFix(
        selectors=("cache key", "tenant namespace"),
        path="cache.py",
        old="return user_id",
        new='return f"{tenant_id}:{user_id}"',
        reason="Namespace cache entries by tenant to prevent cross-tenant collisions.",
    ),
)


class AgentModel(Protocol):
    async def plan(self, issue_title: str, issue_body: str) -> PlanOutput: ...

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput: ...

    async def review(
        self,
        issue_title: str,
        diff: str,
        test_result: SandboxResult,
    ) -> ReviewOutput: ...


class MockAgentModel:
    """Deterministic model for CI and the bundled demo; it never calls a network."""

    async def plan(self, issue_title: str, issue_body: str) -> PlanOutput:
        return PlanOutput(
            summary=f"Diagnose and fix: {issue_title}",
            steps=[
                "Locate implementation and tests related to the issue",
                "Make the smallest behavior-preserving edit",
                "Run the repository test command",
                "Review the diff and test evidence",
            ],
            search_terms=[word.strip(".,:()[]").lower() for word in issue_title.split()[:8]],
            risk_notes=["Do not modify files outside the selected repository"],
        )

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        del plan, reviewer_feedback
        issue_text = f"{issue_title}\n{issue_body}".lower()
        edits: list[FileEdit] = []
        for rule in _DETERMINISTIC_FIXES:
            if not any(selector in issue_text for selector in rule.selectors):
                continue
            for path, content in context.files.items():
                if not path.endswith(rule.path) or rule.old not in content:
                    continue
                fixed = content.replace(rule.old, rule.new, 1)
                edits.append(
                    FileEdit(
                        path=path,
                        content=fixed,
                        reason=rule.reason,
                    )
                )
                break
            if edits:
                break
        return CodeChangeOutput(
            summary=(
                "Applied the deterministic demo fix."
                if edits
                else f"No safe deterministic edit matched: {issue_title} {issue_body[:80]}"
            ),
            edits=edits,
        )

    async def review(
        self,
        issue_title: str,
        diff: str,
        test_result: SandboxResult,
    ) -> ReviewOutput:
        approved = bool(diff.strip()) and test_result.passed
        feedback: list[str] = []
        if not diff.strip():
            feedback.append("No repository change was produced.")
        if not test_result.passed:
            feedback.append("The configured test command did not pass.")
        return ReviewOutput(
            approved=approved,
            summary=(
                f"Change for '{issue_title}' has test evidence."
                if approved
                else "Change requires another bounded iteration."
            ),
            feedback=feedback,
            risk_level="low" if approved else "medium",
        )


class OpenAICompatibleAgentModel:
    def __init__(self, settings: Settings, budget: ModelCallBudget | None = None):
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when MODEL_PROVIDER=openai")
        kwargs: dict[str, object] = {
            "model": settings.model_name,
            "api_key": settings.openai_api_key,
            "temperature": settings.model_temperature,
            # Evaluation owns bounded Retry-After handling so hidden SDK retries cannot
            # evade its call cap. Production retains the existing SDK retry behavior.
            "max_retries": 0 if budget is not None else 2,
            "timeout": 90,
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        self.model = ChatOpenAI(**kwargs)
        self.budget = budget

    async def _structured_invoke(
        self,
        schema: type[_StructuredOutput],
        messages: list[dict[str, Any]],
    ) -> _StructuredOutput:
        if self.budget is None:
            structured = self.model.with_structured_output(schema)
            return await structured.ainvoke(messages)
        structured = self.model.with_structured_output(schema, include_raw=True)
        envelope = await self.budget.call(structured.ainvoke, messages)
        parsing_error = envelope.get("parsing_error")
        if parsing_error is not None:
            raise parsing_error
        parsed = envelope.get("parsed")
        if not isinstance(parsed, schema):
            raise TypeError(f"model did not return a parsed {schema.__name__}")
        return parsed

    async def plan(self, issue_title: str, issue_body: str) -> PlanOutput:
        return await self._structured_invoke(
            PlanOutput,
            [
                {
                    "role": "system",
                    "content": (
                        "You are the planning agent in a software-delivery workflow. "
                        "Produce a short, testable plan. Do not claim to have read code yet."
                    ),
                },
                {"role": "user", "content": f"Title: {issue_title}\n\n{issue_body}"},
            ],
        )

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        editable_paths = (
            tuple(context.files) if context.editable_paths is None else context.editable_paths
        )
        allowed_paths = json.dumps(list(editable_paths), ensure_ascii=False)
        return await self._structured_invoke(
            CodeChangeOutput,
            [
                {
                    "role": "system",
                    "content": (
                        "You are a coding agent operating inside a restricted repository. "
                        "Return complete UTF-8 content only for files that must change. "
                        "Every edit.path must exactly match one of the explicitly allowed "
                        "paths in the user message; do not invent, prefix, or normalize paths. "
                        "Other readable files may appear in context but remain read-only and "
                        "must not appear in edits. If no allowed edit is safe, return no edits. "
                        "Make the smallest change that solves the issue and preserves tests."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Issue: {issue_title}\n{issue_body}\n\n"
                        f"Allowed edit paths (exact JSON array): {allowed_paths}\n"
                        f"Plan: {plan.model_dump_json()}\n"
                        f"Reviewer feedback: {reviewer_feedback}\n\n{context.render()}"
                    ),
                },
            ],
        )

    async def review(
        self,
        issue_title: str,
        diff: str,
        test_result: SandboxResult,
    ) -> ReviewOutput:
        return await self._structured_invoke(
            ReviewOutput,
            [
                {
                    "role": "system",
                    "content": (
                        "You are a read-only advisory reviewer. Approve only when the diff "
                        "addresses the issue, is scoped, and the test evidence passes. Use low "
                        "risk when the only concern is missing targeted evidence or a speculative "
                        "improvement. Use medium/high risk only for a concrete defect visible in "
                        "the supplied diff, and name that defect precisely in feedback."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Issue: {issue_title}\n\nDiff:\n{diff[:50_000]}\n\n"
                        f"Test evidence:\n{test_result.model_dump_json()}"
                    ),
                },
            ],
        )


class BudgetedAgentModel:
    """Count calls for models that do not expose a lower-level response envelope."""

    def __init__(self, model: AgentModel, budget: ModelCallBudget) -> None:
        self.model = model
        self.budget = budget

    async def plan(self, issue_title: str, issue_body: str) -> PlanOutput:
        return await self.budget.call(self.model.plan, issue_title, issue_body)

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        return await self.budget.call(
            self.model.propose_changes,
            issue_title,
            issue_body,
            plan,
            context,
            reviewer_feedback,
        )

    async def review(
        self,
        issue_title: str,
        diff: str,
        test_result: SandboxResult,
    ) -> ReviewOutput:
        return await self.budget.call(self.model.review, issue_title, diff, test_result)


def build_agent_model(
    settings: Settings,
    budget: ModelCallBudget | None = None,
) -> AgentModel:
    if settings.model_provider == "mock":
        model: AgentModel = MockAgentModel()
        return BudgetedAgentModel(model, budget) if budget is not None else model
    if settings.model_provider == "openai":
        return OpenAICompatibleAgentModel(settings, budget=budget)
    raise ValueError(f"Unknown model provider: {settings.model_provider}")
