from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis

from repopilot.db import Database
from repopilot.models import EventView
from repopilot.security import redact_secrets


class EventBus:
    def __init__(self, database: Database, redis: Redis):
        self.database = database
        self.redis = redis

    @staticmethod
    def channel(task_id: str) -> str:
        return f"repopilot:events:{task_id}"

    async def publish(
        self,
        task_id: str,
        *,
        kind: str,
        node: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> EventView:
        safe_message = redact_secrets(message)
        event = await self.database.add_event(
            task_id,
            kind=kind,
            node=node,
            message=safe_message,
            payload=payload or {},
        )
        await self.redis.publish(self.channel(task_id), event.model_dump_json())
        return event

    async def subscribe(self, task_id: str) -> AsyncIterator[str]:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(self.channel(task_id))
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    yield data.decode() if isinstance(data, bytes) else str(data)
        finally:
            await pubsub.unsubscribe(self.channel(task_id))
            await pubsub.aclose()


class JobQueue:
    def __init__(self, redis: Redis, name: str):
        self.redis = redis
        self.name = name

    async def enqueue(self, task_id: str, *, resume: dict[str, Any] | None = None) -> None:
        payload = json.dumps({"task_id": task_id, "resume": resume})
        await self.redis.lpush(self.name, payload)

    async def dequeue(self, wait_seconds: int = 5) -> dict[str, Any] | None:
        result = await self.redis.brpop(self.name, timeout=wait_seconds)
        if result is None:
            return None
        _, payload = result
        if isinstance(payload, bytes):
            payload = payload.decode()
        return json.loads(payload)

    async def request_cancel(self, task_id: str) -> None:
        await self.redis.set(f"repopilot:cancel:{task_id}", "1", ex=3600)

    async def is_cancelled(self, task_id: str) -> bool:
        return bool(await self.redis.exists(f"repopilot:cancel:{task_id}"))

    async def acquire(self, task_id: str, token: str, ttl: int = 900) -> bool:
        return bool(
            await self.redis.set(f"repopilot:lock:{task_id}", token, ex=ttl, nx=True)
        )

    async def release(self, task_id: str, token: str) -> None:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('del', KEYS[1])
        end
        return 0
        """
        await self.redis.eval(script, 1, f"repopilot:lock:{task_id}", token)
