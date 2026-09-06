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

from repopilot.config import Settings, get_settings
from repopilot.db import Database
from repopilot.events import EventBus, JobQueue
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.graph.builder import TaskCancelled
from repopilot.llm import build_agent_model
from repopilot.models import TaskStatus, TaskView
from repopilot.repository import WorkspaceManager
from repopilot.sandbox import build_sandbox

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("repopilot.worker")


def result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "iteration": result.get("iteration"),
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
        self.dependencies = GraphDependencies(
            settings=settings,
            model=build_agent_model(settings),
            workspaces=WorkspaceManager(settings),
            sandbox=build_sandbox(settings),
            publisher=GitHubPublisher(settings),
            events=self.events,
            queue=self.queue,
        )

    async def run_forever(self) -> None:
        logger.info("RepoPilot worker is ready")
        while True:
            job = await self.queue.dequeue(wait_seconds=5)
            if job is None:
                continue
            await self.handle(job)

    async def handle(self, job: dict[str, Any]) -> None:
        task_id = str(job["task_id"])
        token = str(uuid.uuid4())
        if not await self.queue.acquire(task_id, token):
            logger.info("Task %s is already owned by another worker", task_id)
            return
        try:
            task = await self.database.get_task(task_id)
            if task is None:
                return
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                return
            await self.database.update_task(task_id, status=TaskStatus.RUNNING, error=None)
            await self.events.publish(
                task_id,
                kind="task_started",
                node="worker",
                message="Worker started graph execution",
            )
            graph = build_graph(self.dependencies, self.checkpointer)
            config = {"configurable": {"thread_id": task.graph_thread_id}}
            resume = job.get("resume")
            if resume is None:
                graph_input: dict[str, Any] | Command = self._initial_state(task)
            else:
                graph_input = Command(resume=resume)
            result = await graph.ainvoke(graph_input, config=config)
            interrupts = result.get("__interrupt__", [])
            if interrupts:
                payloads = [getattr(item, "value", str(item)) for item in interrupts]
                await self.database.update_task(
                    task_id,
                    status=TaskStatus.AWAITING_APPROVAL,
                    result={"approval_requests": payloads, **result_summary(result)},
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
            await self.database.update_task(
                task_id,
                status=final_status,
                result=result_summary(result),
                error=result.get("error"),
            )
            await self.events.publish(
                task_id,
                kind="task_finished",
                node="worker",
                message=f"Task finished with status {final_status.value}",
            )
        except TaskCancelled as exc:
            await self.database.update_task(task_id, status=TaskStatus.CANCELLED, error=str(exc))
        except Exception as exc:
            logger.exception("Task %s failed", task_id)
            await self.database.update_task(
                task_id,
                status=TaskStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            await self.events.publish(
                task_id,
                kind="task_error",
                node="worker",
                message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            await self.queue.release(task_id, token)

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
