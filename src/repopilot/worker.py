from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from redis.asyncio import Redis
from sqlalchemy import select

from repopilot.approval_models import TestEvidence
from repopilot.approvals import ApprovalConflict, ApprovalStore
from repopilot.candidates import ArtifactStore, Candidate
from repopilot.config import Settings, get_settings
from repopilot.db import Database, TaskRecord, TaskStateConflict
from repopilot.events import EventBus, JobQueue
from repopilot.execution import CleanupFailed, ExecutionControl, ExecutionStopped
from repopilot.generations import GenerationConflict, GenerationCoordinator, SQLGenerationStore
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.llm import build_agent_model, call_policy_from_settings
from repopilot.model_calls import ControlledModelCaller
from repopilot.models import TaskStatus, TaskView
from repopilot.repository import WorkspaceManager
from repopilot.sandbox import build_sandbox

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("repopilot.worker")


def result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "iteration": result.get("iteration"),
        "policy_retry_count": result.get("policy_retry_count", 0),
        "policy_feedback": result.get("policy_feedback"),
        "changed_files": result.get("changed_files", []),
        "initial_retrieval": result.get("initial_retrieval", {}),
        "retrieval": {
            "strategy": result.get("retrieval_strategy"),
            "query_terms": result.get("retrieval_query_terms", []),
            "candidate_count": result.get("retrieval_candidate_count", 0),
            "selected_files": list(result.get("research_files", {})),
            "selected_chars": result.get("retrieval_selected_chars", 0),
            "skipped_for_budget": result.get("retrieval_skipped_for_budget", 0),
            "evidence": result.get("research_evidence", []),
        },
        "review": result.get("review"),
        "test_result": result.get("test_result"),
        "node_metrics": result.get("node_metrics", []),
        "pull_request_url": result.get("pull_request_url"),
        "error": result.get("error"),
        "candidate_id": (result.get("candidate") or {}).get("candidate_id"),
        "generation": (result.get("candidate") or {}).get("generation"),
        "evidence_digest": (result.get("test_evidence") or {}).get("evidence_digest"),
        "approval_decision_id": result.get("approval_decision_id"),
        "publish_receipt": result.get("publish_receipt"),
        "publish_reason": result.get("publish_reason"),
    }


class Worker:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        redis: Redis,
        checkpointer: Any,
    ):
        self.settings = settings
        self.database = database
        self.redis = redis
        self.events = EventBus(database, redis)
        self.queue = JobQueue(redis, settings.queue_name)
        self.checkpointer = checkpointer
        self.workspaces = WorkspaceManager(settings)
        self.sandbox = build_sandbox(settings)
        self.publisher = GitHubPublisher(settings)
        self.approvals = ApprovalStore(database)

    async def _dependencies(self, task_id: str) -> GraphDependencies:
        policy = call_policy_from_settings(self.settings)
        ledger = self.database.model_call_ledger(task_id)
        await ledger.ensure(policy)
        reconciled = await ledger.reconcile_incomplete()
        if reconciled:
            logger.warning(
                "Task %s conservatively marked %d in-flight model call(s) unknown",
                task_id,
                reconciled,
            )
        call_control = ControlledModelCaller(ledger, policy)
        generations = SQLGenerationStore(self.database)
        recovered = await GenerationCoordinator(
            ArtifactStore(self.settings.workspace_root / ".artifacts"), generations, self.workspaces
        ).recover(task_id)
        if recovered:
            logger.info("Task %s staging reconciliation: %s", task_id, recovered)
        return GraphDependencies(
            settings=self.settings,
            model=build_agent_model(self.settings, call_control=call_control),
            workspaces=self.workspaces,
            sandbox=self.sandbox,
            publisher=self.publisher,
            events=self.events,
            queue=self.queue,
            approvals=self.approvals,
            generations=generations,
        )

    async def run_forever(self) -> None:
        await self.reconcile_staging()
        await self.report_pending_stops()
        logger.info("RepoPilot worker is ready")
        sweeper = asyncio.create_task(self._sweep_deadlines())
        try:
            while True:
                job = await self.queue.dequeue(wait_seconds=5)
                if job is None:
                    continue
                await self.handle(job)
        finally:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)

    async def _sweep_deadlines(self) -> None:
        # Waiting tasks must expire even while this Worker is executing another task.
        while True:
            try:
                for stopped in await self.database.expire_waiting_tasks():
                    await self.events.notify_control(stopped.id)
            except Exception as error:
                logger.warning("Waiting deadline scan unavailable: %s", type(error).__name__)
            await asyncio.sleep(1)

    async def report_pending_stops(self) -> None:
        async with self.database.sessions() as session:
            pending = (
                (
                    await session.execute(
                        select(TaskRecord).where(TaskRecord.status == TaskStatus.CANCELLING.value)
                    )
                )
                .scalars()
                .all()
            )
            snapshots = [task.to_view() for task in pending]
        for task in snapshots:
            # A restarted process cannot prove its predecessor's quiescence. In
            # particular, a persisted POSIX PID might already have been reused.
            await self.database.confirm_stop(
                task.id,
                expected_version=task.state_version,
                cleanup_pending=task.cleanup_pending
                or task.execution_resources
                or [
                    {
                        "kind": "execution_attempt",
                        "action": "verify_quiescence",
                        "error": "worker_restart",
                    }
                ],
            )

    async def reconcile_staging(self) -> dict[str, list[dict[str, Any]]]:
        """Classify retained data on startup; never enqueue or adopt a candidate."""
        async with self.database.sessions() as session:
            task_ids = (
                (
                    await session.execute(
                        select(TaskRecord.id).where(TaskRecord.status == TaskStatus.RUNNING.value)
                    )
                )
                .scalars()
                .all()
            )
        coordinator = GenerationCoordinator(
            ArtifactStore(self.settings.workspace_root / ".artifacts"),
            SQLGenerationStore(self.database),
            self.workspaces,
        )
        reports = {}
        for task_id in task_ids:
            reports[task_id] = await coordinator.recover(task_id)
            logger.info("Task %s startup staging reconciliation: %s", task_id, reports[task_id])
        return reports

    async def handle(self, job: dict[str, Any]) -> None:
        task_id = str(job["task_id"])
        token = str(uuid.uuid4())
        active_task: TaskView | None = None
        control: ExecutionControl | None = None
        watcher: asyncio.Task | None = None
        result = None
        if not await self.queue.acquire(task_id, token):
            logger.info("Task %s is already owned by another worker", task_id)
            return
        try:
            task = await self.database.get_task(task_id)
            if task is None:
                return
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                return
            if task.status is not TaskStatus.QUEUED:
                logger.info(
                    "Task %s is %s, not queued; ignoring delivery",
                    task_id,
                    task.status.value,
                )
                return
            if task.remaining_seconds == 0:
                await self.database.request_stop(task_id, "deadline_exceeded")
                return
            active_task = await self.database.transition_task(
                task_id,
                expected_status=TaskStatus.QUEUED,
                expected_version=task.state_version,
                status=TaskStatus.RUNNING,
                error=None,
            )
            await self.events.publish(
                task_id,
                kind="task_started",
                node="worker",
                message="Worker started graph execution",
            )

            async def persist_resources(resources):
                await self.database.execution_resources(
                    task_id, active_task.state_version, resources
                )

            control = ExecutionControl(
                task_id,
                active_task.remaining_seconds,
                reconcile_seconds=self.settings.publication_reconcile_seconds,
                resource_hook=persist_resources,
            )
            watcher = asyncio.create_task(self._watch_execution(active_task, control))
            with control.bind():
                dependencies = await control.run(self._dependencies(task_id))
                graph = build_graph(dependencies, self.checkpointer)
                config = {"configurable": {"thread_id": task.graph_thread_id}}
                # A queue message is a hint. Only the committed decision may resume a
                # production approval; duplicate initial deliveries also resolve here.
                resume = await self.approvals.resume_for_running_task(active_task)
                if job.get("resume") is not None and resume is None:
                    raise ApprovalConflict("legacy_approval_requires_revalidation")
                if resume is None:
                    graph_input: dict[str, Any] | Command = self._initial_state(task)
                else:
                    graph_input = Command(resume=resume)
                result = await graph.ainvoke(graph_input, config=config)
                control.check()
                interrupts = result.get("__interrupt__", [])
                if interrupts:
                    payloads = [getattr(item, "value", str(item)) for item in interrupts]
                    active_task = await self.approvals.open_request(
                        task_id,
                        expected_version=active_task.state_version,
                        candidate=Candidate.model_validate(result["candidate"]),
                        evidence=TestEvidence.model_validate(result["test_evidence"]),
                        summary={"approval_requests": payloads, **result_summary(result)},
                    )
                    await self.events.publish(
                        task_id,
                        kind="approval_required",
                        node="approval",
                        message="Graph checkpointed and is waiting for human approval",
                        payload={"requests": payloads},
                    )
                    return

                final_status = {
                    "completed": TaskStatus.COMPLETED,
                    "cancelled": TaskStatus.CANCELLED,
                    "failed": TaskStatus.FAILED,
                }.get(result.get("status"), TaskStatus.FAILED)
                active_task = await self.database.transition_task(
                    task_id,
                    expected_status=TaskStatus.RUNNING,
                    expected_version=active_task.state_version,
                    status=final_status,
                    result=result_summary(result),
                    error=result.get("error"),
                    expected_candidate_id=(result.get("candidate") or {}).get("candidate_id"),
                )
                await self.events.publish(
                    task_id,
                    kind="task_finished",
                    node="worker",
                    message=f"Task finished with status {final_status.value}",
                )
        except (TaskStateConflict, GenerationConflict) as exc:
            logger.info("Discarding stale task state update: %s", exc)
        except ExecutionStopped as exc:
            if exc.scope == "task":
                await self.database.request_stop(task_id, exc.reason)
            else:
                await self.events.publish(
                    task_id,
                    kind="attempt_aborted",
                    node="worker",
                    message="Only this execution attempt was stopped",
                    payload={"scope": "attempt"},
                )
        except CleanupFailed as exc:
            if control:
                control.cleanup_pending.append(exc.resource)
            await self.database.request_stop(task_id, "cleanup_failed")
        except asyncio.CancelledError:
            if control:
                control.abort_attempt()
                control.cleanup_pending.append(
                    {
                        "kind": "execution_attempt",
                        "action": "verify_quiescence",
                        "error": "worker_interrupted",
                    }
                )
            raise
        except Exception as exc:
            logger.exception("Task %s failed", task_id)
            if control and control.resources:
                control.cleanup_pending.extend(
                    {**resource, "action": "verify_cleanup", "error": type(exc).__name__}
                    for resource in control.resources
                )
                await self.database.request_stop(task_id, "cleanup_failed")
            elif active_task is not None and active_task.status is TaskStatus.RUNNING:
                try:
                    await self.database.transition_task(
                        task_id,
                        expected_status=TaskStatus.RUNNING,
                        expected_version=active_task.state_version,
                        status=TaskStatus.FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except TaskStateConflict as conflict:
                    logger.info("Failure reporting lost a state race: %s", conflict)
                else:
                    await self.events.publish(
                        task_id,
                        kind="task_error",
                        node="worker",
                        message=f"{type(exc).__name__}: {exc}",
                    )
        finally:
            try:
                if watcher:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                if control and active_task:
                    current = await self.database.get_task(task_id)
                    if (
                        current
                        and current.status is TaskStatus.CANCELLING
                        and current.state_version == active_task.state_version + 1
                    ):
                        ledger = self.database.model_call_ledger(task_id)
                        await ledger.ensure(call_policy_from_settings(self.settings))
                        await ledger.reconcile_incomplete()
                        await self.database.confirm_stop(
                            task_id,
                            expected_version=current.state_version,
                            cleanup_pending=control.cleanup_pending,
                            result=result_summary(result) if result else None,
                        )
                        await self.events.notify_control(task_id)
            finally:
                await self.queue.release(task_id, token)

    async def _watch_execution(self, active: TaskView, control: ExecutionControl) -> None:
        try:
            while True:
                current = await self.database.get_task(active.id)
                if current is None or current.state_version not in {
                    active.state_version,
                    active.state_version + 1,
                }:
                    control.abort_attempt()
                    return
                if current.status is TaskStatus.CANCELLING:
                    control.stop(current.stop_reason or "cancel_requested")
                    await self.events.notify_control(active.id)
                    return
                if current.status is not TaskStatus.RUNNING:
                    return
                if control.remaining() == 0:
                    await self.database.request_stop(active.id, "deadline_exceeded")
                    return
                await asyncio.sleep(self.settings.cancellation_poll_seconds)

        except asyncio.CancelledError:
            raise
        except Exception as error:
            control.abort_attempt()
            logger.warning(
                "Task %s execution control unavailable: %s", active.id, type(error).__name__
            )

    @staticmethod
    def _initial_state(task: TaskView) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "repository_url": task.repository_url,
            "issue_title": task.issue_title,
            "issue_body": task.issue_body,
            "base_branch": task.base_branch,
            "test_command": task.test_command,
            "max_iterations": task.max_iterations,
            "reviewer_feedback": [],
            "iteration": 0,
            "policy_retry_count": 0,
            "policy_feedback": None,
            "policy_error_code": None,
            "pending_changes": None,
            "node_metrics": [],
            "status": "queued",
        }


@asynccontextmanager
async def checkpointer_context(settings: Settings):
    if settings.langgraph_database_url:
        async with AsyncPostgresSaver.from_conn_string(
            settings.langgraph_database_url
        ) as checkpointer:
            await checkpointer.setup()
            yield checkpointer
    else:
        yield InMemorySaver()


async def async_main() -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    await database.setup()
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    await redis.ping()
    try:
        async with checkpointer_context(settings) as checkpointer:
            await Worker(settings, database, redis, checkpointer).run_forever()
    finally:
        await redis.aclose()
        await database.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
