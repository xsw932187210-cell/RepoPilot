from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from langchain_openai import ChatOpenAI

from repopilot.config import Settings
from repopilot.model_calls import (
    CALL_CONTROL_VERSION,
    CallIdentity,
    CallPolicy,
    ControlledModelCaller,
    InvalidProviderResponse,
    ModelCallBudget,
)
from repopilot.models import CodeChangeOutput, FileEdit, PlanOutput, ReviewOutput, SandboxResult
from repopilot.repository import RepositoryContext

_StructuredOutput = TypeVar("_StructuredOutput", PlanOutput, CodeChangeOutput, ReviewOutput)
_CallControl = ControlledModelCaller | ModelCallBudget

OPENAI_STRUCTURED_ADAPTER_VERSION = "langchain-openai-structured-v1"
MOCK_ADAPTER_VERSION = "deterministic-mock-v1"


def call_policy_from_settings(settings: Settings) -> CallPolicy:
    return CallPolicy(
        max_calls=settings.model_max_calls,
        request_timeout_seconds=settings.model_request_timeout_seconds,
        max_rate_limit_retries=settings.model_max_rate_limit_retries,
        max_transient_retries=settings.model_max_transient_retries,
        base_backoff_seconds=settings.model_retry_base_seconds,
        max_retry_wait_seconds=settings.model_max_retry_wait_seconds,
        max_total_backoff_seconds=settings.model_max_total_backoff_seconds,
        max_total_tokens=settings.model_max_total_tokens or None,
        max_output_tokens=settings.model_max_output_tokens,
        version=CALL_CONTROL_VERSION,
    )


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
    def __init__(
        self,
        settings: Settings,
        budget: ModelCallBudget | None = None,
        *,
        call_control: ControlledModelCaller | None = None,
    ):
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when MODEL_PROVIDER=openai")
        if budget is not None and call_control is not None:
            raise ValueError("provide either budget or call_control, not both")
        if budget is None and call_control is None:
            policy = call_policy_from_settings(settings)
            budget = ModelCallBudget(
                policy.max_calls,
                max_rate_limit_retries=policy.max_rate_limit_retries,
                max_transient_retries=policy.max_transient_retries,
                base_backoff_seconds=policy.base_backoff_seconds,
                max_backoff_seconds=policy.max_retry_wait_seconds,
                max_total_backoff_seconds=policy.max_total_backoff_seconds,
                request_timeout_seconds=policy.request_timeout_seconds,
                max_total_tokens=policy.max_total_tokens,
                max_output_tokens=policy.max_output_tokens,
                policy_version=policy.version,
            )
        kwargs: dict[str, object] = {
            "model": settings.model_name,
            "api_key": settings.openai_api_key,
            "temperature": settings.model_temperature,
            # The controlled boundary owns every retry so SDK attempts cannot evade the
            # persistent call budget.
            "max_retries": 0,
            "timeout": settings.model_request_timeout_seconds,
            "max_tokens": settings.model_max_output_tokens,
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        self.model = ChatOpenAI(**kwargs)
        self.call_control: _CallControl = call_control or budget
        self.identity = CallIdentity(
            provider="openai",
            model=settings.model_name,
            adapter_version=OPENAI_STRUCTURED_ADAPTER_VERSION,
            request_schema_version="assigned-per-request",
        )

    async def _structured_invoke(
        self,
        schema: type[_StructuredOutput],
        messages: list[dict[str, Any]],
        role: str,
    ) -> _StructuredOutput:
        structured = self.model.with_structured_output(schema, include_raw=True)

        def validate_response(response: object) -> None:
            if not isinstance(response, dict):
                raise InvalidProviderResponse()
            if response.get("parsing_error") is not None:
                raise InvalidProviderResponse()
            if not isinstance(response.get("parsed"), schema):
                raise InvalidProviderResponse()

        identity = CallIdentity(
            provider=self.identity.provider,
            model=self.identity.model,
            adapter_version=self.identity.adapter_version,
            request_schema_version=f"{schema.__name__}-v1",
        )
        envelope = await self.call_control.call(
            structured.ainvoke,
            messages,
            call_identity=identity,
            call_role=role,
            response_validator=validate_response,
        )
        parsed = envelope["parsed"]
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
            "planner",
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
            "coder",
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
            "reviewer",
        )


class BudgetedAgentModel:
    """Count calls for models that do not expose a lower-level response envelope."""

    def __init__(
        self,
        model: AgentModel,
        call_control: _CallControl,
        *,
        provider: str = "mock",
        model_name: str = "deterministic-mock-v1",
        adapter_version: str = MOCK_ADAPTER_VERSION,
    ) -> None:
        self.model = model
        self.call_control = call_control
        self.provider = provider
        self.model_name = model_name
        self.adapter_version = adapter_version

    def _identity(self, schema: type[_StructuredOutput]) -> CallIdentity:
        return CallIdentity(
            provider=self.provider,
            model=self.model_name,
            adapter_version=self.adapter_version,
            request_schema_version=f"{schema.__name__}-v1",
        )

    async def plan(self, issue_title: str, issue_body: str) -> PlanOutput:
        return await self.call_control.call(
            self.model.plan,
            issue_title,
            issue_body,
            call_identity=self._identity(PlanOutput),
            call_role="planner",
        )

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        return await self.call_control.call(
            self.model.propose_changes,
            issue_title,
            issue_body,
            plan,
            context,
            reviewer_feedback,
            call_identity=self._identity(CodeChangeOutput),
            call_role="coder",
        )

    async def review(
        self,
        issue_title: str,
        diff: str,
        test_result: SandboxResult,
    ) -> ReviewOutput:
        return await self.call_control.call(
            self.model.review,
            issue_title,
            diff,
            test_result,
            call_identity=self._identity(ReviewOutput),
            call_role="reviewer",
        )


def build_agent_model(
    settings: Settings,
    budget: ModelCallBudget | None = None,
    *,
    call_control: ControlledModelCaller | None = None,
) -> AgentModel:
    if budget is not None and call_control is not None:
        raise ValueError("provide either budget or call_control, not both")
    control: _CallControl | None = call_control or budget
    if settings.model_provider == "mock":
        model: AgentModel = MockAgentModel()
        return BudgetedAgentModel(model, control) if control is not None else model
    if settings.model_provider == "openai":
        return OpenAICompatibleAgentModel(
            settings,
            budget=budget,
            call_control=call_control,
        )
    raise ValueError(f"Unknown model provider: {settings.model_provider}")
