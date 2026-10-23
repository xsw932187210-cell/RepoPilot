"""Test-only process boundary; never imported by the production Worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


async def run(args: argparse.Namespace) -> None:
    from redis.asyncio import Redis
    from sqlalchemy.engine import make_url

    from repopilot.config import Settings
    from repopilot.db import Database
    from repopilot.models import TaskStatus
    from repopilot.worker import Worker, checkpointer_context, result_summary

    settings = Settings(
        _env_file=None,
        model_provider="mock",
        openai_api_key="",
        github_token="",
        github_write_enabled=False,
        model_max_calls=2 if args.scenario == "budget" else 8,
    )
    assert make_url(settings.database_url).database in {
        "repopilot_ch02b_test", "repopilot_ch03a_test", "repopilot_ch05a_test",
    }
    database = Database(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    try:
        async with checkpointer_context(settings) as saver:
            worker = Worker(settings, database, redis, saver)
            task = await database.get_task(args.task_id)
            assert task is not None
            if args.phase == "legacy":
                # PYTHONPATH points at the pinned pre-CH-02B source tree.
                assert "pending_changes" not in Worker._initial_state(task)
                await worker.handle({"task_id": task.id})
                task = await database.get_task(task.id)
                assert task is not None and task.status is TaskStatus.AWAITING_APPROVAL
                return

            from repopilot.graph import build_graph
            from repopilot.llm import BudgetedAgentModel, MockAgentModel
            from repopilot.models import FileEdit
            from repopilot.policy_feedback import content_sha256

            class InjectedModel(MockAgentModel):
                old_digest: str | None = None

                async def propose_changes(self, title, body, plan, context, feedback):
                    changes = await super().propose_changes(title, body, plan, context, feedback)
                    denied_before = any("policy-feedback-v1" in item for item in feedback)
                    if args.scenario == "stale" and denied_before:
                        assert self.old_digest is not None
                        changes.edits[0].expected_content_sha256 = self.old_digest
                    elif not denied_before or args.scenario == "repeat":
                        path = next(path for path in context.files if path.startswith("tests/"))
                        content = context.files[path]
                        if args.scenario == "redaction":
                            # Known tokens now stop before checkpointing (CH-06C).
                            # Keep the old hard-exit probe focused on path policy.
                            path = "/untrusted-host/private-marker/source.py"
                        changes.edits.append(
                            FileEdit(
                                path=path,
                                content=content + "\n# forbidden CH-02B probe edit\n",
                                expected_content_sha256=content_sha256(content),
                            )
                        )
                    return changes

            dependencies = await worker._dependencies(task.id)
            assert isinstance(dependencies.model, BudgetedAgentModel)
            injected = InjectedModel()
            dependencies.model = BudgetedAgentModel(injected, dependencies.model.call_control)
            graph = build_graph(dependencies, saver)
            config = {"configurable": {"thread_id": task.graph_thread_id}}
            if args.phase == "pause":
                await database.transition_task(
                    task.id,
                    expected_status=TaskStatus.QUEUED,
                    expected_version=task.state_version,
                    status=TaskStatus.RUNNING,
                )
                await graph.ainvoke(
                    Worker._initial_state(task),
                    config=config,
                    interrupt_after=["apply_policy"],
                    durability="sync",
                )
                snapshot = await graph.aget_state(config)
                assert snapshot.next == ("coder",)
                assert snapshot.values["policy_retry_count"] == 1
                assert snapshot.values["policy_feedback"] is not None
                metrics = await database.get_model_call_metrics(task.id)
                assert metrics is not None and metrics["reserved_calls"] == 2
                # The saver has committed before a hard exit; finally blocks cannot
                # save in-memory state or repair this deliberately killed process.
                os._exit(86)

            assert task.status is TaskStatus.RUNNING
            snapshot = await graph.aget_state(config)
            assert snapshot.values["policy_retry_count"] == 1
            injected.old_digest = content_sha256(snapshot.values["research_files"]["calculator.py"])
            if args.scenario == "stale":
                # The original checkout is no longer the read/write view. A proposal
                # based on its external edits must not match the frozen generation.
                injected.old_digest = content_sha256(
                    (Path(snapshot.values["workspace"]) / "calculator.py").read_text()
                )
            result = await graph.ainvoke(None, config=config, durability="sync")
            interrupts = result.get("__interrupt__", [])
            summary = result_summary(result)
            if interrupts:
                from repopilot.approval_models import TestEvidence
                from repopilot.candidates import Candidate

                status = TaskStatus.AWAITING_APPROVAL
                summary["approval_requests"] = [item.value for item in interrupts]
                await worker.approvals.open_request(
                    task.id,
                    expected_version=task.state_version,
                    candidate=Candidate.model_validate(result["candidate"]),
                    evidence=TestEvidence.model_validate(result["test_evidence"]),
                    summary=summary,
                )
            else:
                assert result["status"] == "failed"
                status = TaskStatus.FAILED
                await database.transition_task(
                    task.id,
                    expected_status=TaskStatus.RUNNING,
                    expected_version=task.state_version,
                    status=status,
                    result=summary,
                    error=result.get("error"),
                )
            print(json.dumps({"scenario": args.scenario, "status": status.value}))
    finally:
        await redis.aclose()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("pause", "resume", "legacy"))
    parser.add_argument("scenario", choices=("once", "repeat", "budget", "stale", "redaction"))
    parser.add_argument("task_id")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
