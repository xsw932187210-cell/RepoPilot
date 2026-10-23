"""Guard untrusted graph input and disable automatic external tracing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from langgraph.types import Command
from langsmith import tracing_context

from repopilot.data_boundary import require_secret_free
from repopilot.security import redact_secrets


def graph_input(value):
    if isinstance(value, Command):
        # Resume bindings are authority data: do not silently change a saved
        # decision. API feedback is already redacted before its decision is saved.
        require_secret_free(value.resume)
        require_secret_free(value.update)
        return replace(value)
    if isinstance(value, Mapping):
        result = dict(value)
        for name in ("issue_title", "issue_body", "human_feedback"):
            if isinstance(result.get(name), str):
                result[name] = redact_secrets(result[name])
        require_secret_free(result)
        return result
    return value


class PrivateExecutionGraph:
    def __init__(self, graph):
        self.graph = graph

    def __getattr__(self, name):
        return getattr(self.graph, name)

    async def _input(self, value, args, kwargs):
        value = graph_input(value)
        config = kwargs.get("config") or (args[0] if args else None)
        if config:
            snapshot = await self.graph.aget_state(config)
            require_secret_free(snapshot.values)
        return value

    async def ainvoke(self, value, *args, **kwargs):
        with tracing_context(enabled=False):
            value = await self._input(value, args, kwargs)
            return await self.graph.ainvoke(value, *args, **kwargs)

    async def astream(self, value, *args, **kwargs):
        with tracing_context(enabled=False):
            value = await self._input(value, args, kwargs)
            async for item in self.graph.astream(value, *args, **kwargs):
                yield item
