"""Test-owned process boundaries. Hard exits deliberately bypass all cleanup."""

from __future__ import annotations

import argparse
import asyncio
import os


async def run(args):
    from redis.asyncio import Redis
    from sqlalchemy.engine import make_url

    from repopilot.config import Settings
    from repopilot.db import Database
    from repopilot.worker import Worker, checkpointer_context

    settings = Settings(_env_file=None, github_write_enabled=False, model_provider="mock")
    name = make_url(settings.database_url).database
    assert name == "repopilot_ch05a_test" or name.startswith("repopilot_ch05a_migration_")
    db = Database(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    try:
        if args.phase == "reconcile":
            await Worker(settings, db, redis, None).reconcile_staging()
            return
        if args.phase == "legacy_approval":
            # The caller supplies the pinned CH-03A PYTHONPATH and a revision-0004 DB.
            assert "generations" not in Worker.__init__.__code__.co_names
            await db.setup()
            async with checkpointer_context(settings) as saver:
                await Worker(settings, db, redis, saver).handle({"task_id": args.task_id})
            return

        from repopilot.approval_models import TestEvidence
        from repopilot.approvals import ApprovalStore
        from repopilot.candidates import ArtifactStore, Candidate
        from repopilot.generations import GenerationCoordinator, SQLGenerationStore
        from repopilot.graph import build_graph
        from repopilot.models import FileEdit, TaskStatus
        from repopilot.repository import WorkspaceManager
        from repopilot.worker import result_summary

        if args.phase.startswith("graph_"):
            async with checkpointer_context(settings) as saver:
                worker = Worker(settings, db, redis, saver)
                task = await db.get_task(args.task_id)
                deps = await worker._dependencies(task.id)

                class ExitAfterCommit(SQLGenerationStore):
                    async def commit(self, parent, operation_id, candidate):
                        saved = await super().commit(parent, operation_id, candidate)
                        if parent is not None:
                            os._exit(91)
                        return saved

                if args.phase == "graph_pause":
                    deps.generations = ExitAfterCommit(db)
                    task = await db.transition_task(
                        task.id,
                        expected_status=task.status,
                        expected_version=task.state_version,
                        status=TaskStatus.RUNNING,
                    )
                graph = build_graph(deps, saver)
                config = {"configurable": {"thread_id": task.graph_thread_id}}
                result = await graph.ainvoke(
                    Worker._initial_state(task) if args.phase == "graph_pause" else None,
                    config=config,
                    durability="sync",
                )
                assert result["__interrupt__"]
                await worker.approvals.open_request(
                    task.id,
                    expected_version=task.state_version,
                    candidate=Candidate.model_validate(result["candidate"]),
                    evidence=TestEvidence.model_validate(result["test_evidence"]),
                    summary=result_summary(result),
                )
                return

        manager = WorkspaceManager(settings)
        workspace = settings.workspace_root / args.task_id
        parent = await ApprovalStore(db).candidate(args.task_id, args.parent_id)
        artifacts = ArtifactStore(settings.workspace_root / ".artifacts")
        with artifacts.materialize(parent) as snapshot:
            context = {
                path: (snapshot / path).read_text() for path in ("calculator.py", "helper.py")
            }
        edits = [
            FileEdit(
                path="calculator.py",
                content=context["calculator.py"].replace("return a - b", "return a + b"),
            ),
            FileEdit(path="helper.py", content="VALUE = 1\n"),
        ]
        if args.phase == "partial":
            original = manager._write_prepared_file
            count = 0

            def partial(*arguments):
                nonlocal count
                count += 1
                if count == 2:
                    os._exit(88)
                return original(*arguments)

            manager._write_prepared_file = partial

        def fault(phase):
            if phase == args.phase:
                os._exit({"sealed": 89, "committed": 90}[phase])

        await GenerationCoordinator(artifacts, SQLGenerationStore(db), manager, fault=fault).apply(
            parent,
            edits,
            expected_contents=context,
            base_repository=workspace,
        )
    finally:
        await redis.aclose()
        await db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=(
            "partial",
            "sealed",
            "committed",
            "success",
            "graph_pause",
            "graph_resume",
            "legacy_approval",
            "reconcile",
        ),
    )
    parser.add_argument("task_id")
    parser.add_argument("--parent-id", default="")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
