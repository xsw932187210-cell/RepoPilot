from __future__ import annotations

from typing import Protocol

from langchain_openai import ChatOpenAI

from repopilot.config import Settings
from repopilot.models import CodeChangeOutput, FileEdit, PlanOutput, ReviewOutput, SandboxResult
from repopilot.repository import RepositoryContext


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
        edits: list[FileEdit] = []
        for path, content in context.files.items():
            if path.endswith("calculator.py") and "return a - b" in content:
                fixed = content.replace("return a - b", "return a + b", 1)
                edits.append(
                    FileEdit(
                        path=path,
                        content=fixed,
                        reason=(
                            "Correct the add function to perform addition rather than subtraction."
                        ),
                    )
                )
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
    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when MODEL_PROVIDER=openai")
        kwargs: dict[str, object] = {
            "model": settings.model_name,
            "api_key": settings.openai_api_key,
            "temperature": settings.model_temperature,
            "max_retries": 2,
            "timeout": 90,
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        self.model = ChatOpenAI(**kwargs)

    async def plan(self, issue_title: str, issue_body: str) -> PlanOutput:
        structured = self.model.with_structured_output(PlanOutput)
        return await structured.ainvoke(
            [
                {
                    "role": "system",
                    "content": (
                        "You are the planning agent in a software-delivery workflow. "
                        "Produce a short, testable plan. Do not claim to have read code yet."
                    ),
                },
                {"role": "user", "content": f"Title: {issue_title}\n\n{issue_body}"},
            ]
        )

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        structured = self.model.with_structured_output(CodeChangeOutput)
        return await structured.ainvoke(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a coding agent operating inside a restricted repository. "
                        "Return complete UTF-8 content only for files that must change. "
                        "Use only paths visible in the supplied repository context. "
                        "Make the smallest change that solves the issue and preserves tests."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Issue: {issue_title}\n{issue_body}\n\n"
                        f"Plan: {plan.model_dump_json()}\n"
                        f"Reviewer feedback: {reviewer_feedback}\n\n{context.render()}"
                    ),
                },
            ]
        )

    async def review(
        self,
        issue_title: str,
        diff: str,
        test_result: SandboxResult,
    ) -> ReviewOutput:
        structured = self.model.with_structured_output(ReviewOutput)
        return await structured.ainvoke(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a read-only reviewer. Approve only when the diff addresses "
                        "the issue, is scoped, and the test evidence passes."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Issue: {issue_title}\n\nDiff:\n{diff[:50_000]}\n\n"
                        f"Test evidence:\n{test_result.model_dump_json()}"
                    ),
                },
            ]
        )


def build_agent_model(settings: Settings) -> AgentModel:
    if settings.model_provider == "mock":
        return MockAgentModel()
    if settings.model_provider == "openai":
        return OpenAICompatibleAgentModel(settings)
    raise ValueError(f"Unknown model provider: {settings.model_provider}")
