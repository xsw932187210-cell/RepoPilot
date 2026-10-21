"""One execution scope for cancellation, deadlines and retained cleanup evidence."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_current: ContextVar[ExecutionControl | None] = ContextVar("execution_control", default=None)


class ExecutionStopped(RuntimeError):
    def __init__(self, reason: str, scope: str = "task"):
        self.reason, self.scope = reason, scope
        super().__init__(reason)


class CleanupFailed(RuntimeError):
    def __init__(self, resource: dict[str, Any]):
        self.resource = resource
        super().__init__("execution_resource_cleanup_failed")


async def join_task(task):
    """Repeated caller cancellation cannot detach cleanup or a blocking thread."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def drain_thread(function, *args, **kwargs):
    """Cancellation must not leave a filesystem/SDK thread running unobserved."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await join_task(task)
        raise


def current_control() -> ExecutionControl | None:
    return _current.get()


class ExecutionControl:
    def __init__(
        self,
        task_id: str,
        remaining_seconds: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        reconcile_seconds: float = 10,
        resource_hook: Callable[[list[dict]], Awaitable[None]] | None = None,
    ):
        self.task_id = task_id
        self.clock = clock
        self.deadline = None if remaining_seconds is None else clock() + remaining_seconds
        self.reconcile_seconds = reconcile_seconds
        self.reason: str | None = None
        self.scope = "task"
        self.signal = asyncio.Event()
        self.stopped_clock: float | None = None
        self.resources: list[dict] = []
        self.cleanup_pending: list[dict] = []
        self.resource_hook = resource_hook
        self.loop = asyncio.get_running_loop()

    @contextmanager
    def bind(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    def stop(self, reason: str = "cancel_requested", *, scope: str = "task") -> None:
        if self.reason is None:
            self.reason, self.scope = reason, scope
            self.stopped_clock = self.clock()
            self.loop.call_soon_threadsafe(self.signal.set)

    def abort_attempt(self) -> None:
        """Future lease loss stops this attempt without cancelling the SQL task."""
        self.stop("attempt_aborted", scope="attempt")

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        value = max(0.0, self.deadline - self.clock())
        if value == 0:
            self.stop("deadline_exceeded")
        return value

    def check(self) -> None:
        self.remaining()
        if self.reason:
            raise ExecutionStopped(self.reason, self.scope)

    def timeout(self, configured: float) -> float:
        self.check()
        remaining = self.remaining()
        return configured if remaining is None else min(configured, remaining)

    def reconciliation_timeout(self, configured: float) -> float:
        self.remaining()
        if self.reason:
            return max(
                0.0, min(configured, self.reconcile_seconds - (self.clock() - self.stopped_clock))
            )
        return self.timeout(configured)

    async def track(self, resource: dict, *, remove: bool = False) -> None:
        if remove:
            self.resources = [item for item in self.resources if item != resource]
        elif resource not in self.resources:
            self.resources.append(resource)
        if self.resource_hook:
            await self.resource_hook(list(self.resources))

    def track_from_thread(self, resource: dict, *, remove: bool = False) -> None:
        asyncio.run_coroutine_threadsafe(self.track(resource, remove=remove), self.loop).result()

    async def run[T](self, operation: Awaitable[T], *, limit_seconds: float | None = None) -> T:
        """Interrupt an operation, then await its resource cleanup before returning."""
        task = asyncio.ensure_future(operation)
        signal = asyncio.create_task(self.signal.wait())
        try:
            self.check()
            remaining = self.remaining()
            limit = (
                remaining
                if limit_seconds is None
                else (limit_seconds if remaining is None else min(limit_seconds, remaining))
            )
            done, _ = await asyncio.wait(
                {task, signal}, timeout=limit, return_when=asyncio.FIRST_COMPLETED
            )
            self.remaining()
            if task in done:
                return task.result()
            if not self.reason:
                # A per-operation timeout may be shorter than the task deadline.
                raise TimeoutError("execution_operation_timeout")
            raise ExecutionStopped(self.reason, self.scope)
        finally:
            signal.cancel()
            await asyncio.gather(signal, return_exceptions=True)
            if not task.done():
                task.cancel()
                # CleanupFailed must override a stop signal: stopped is not confirmed.
                try:
                    await join_task(task)
                except asyncio.CancelledError:
                    pass


async def controlled[T](awaitable: Awaitable[T], limit_seconds: float) -> T:
    control = current_control()
    if control:
        return await control.run(awaitable, limit_seconds=limit_seconds)
    return await asyncio.wait_for(awaitable, timeout=limit_seconds)
