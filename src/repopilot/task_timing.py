"""Persisted phase accounting; wall timestamps survive Worker restarts."""

from datetime import UTC, datetime, timedelta


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def phase_totals(task, now: datetime) -> dict[str, float]:
    totals = {
        key: float(getattr(task, key, 0) or 0)
        for key in (
            "execution_elapsed_seconds",
            "queue_elapsed_seconds",
            "approval_elapsed_seconds",
        )
    }
    phase = {
        "running": "execution_elapsed_seconds",
        "queued": "queue_elapsed_seconds",
        "awaiting_approval": "approval_elapsed_seconds",
    }.get(task.status)
    started = getattr(task, "phase_started_at", None)
    if phase and started:
        totals[phase] += max(0.0, (aware(now) - aware(started)).total_seconds())
    return totals


def remaining_seconds(task, now: datetime) -> float | None:
    if task.execution_timeout_seconds is None:
        return None
    totals = phase_totals(task, now)
    elapsed = totals["execution_elapsed_seconds"]
    if task.deadline_includes_queue:
        elapsed += totals["queue_elapsed_seconds"]
    if task.deadline_includes_approval:
        elapsed += totals["approval_elapsed_seconds"]
    return max(0.0, task.execution_timeout_seconds - elapsed)


def deadline_at(task, now: datetime) -> datetime | None:
    remaining = remaining_seconds(task, now)
    counts = (
        task.status == "running"
        or (task.status == "queued" and task.deadline_includes_queue)
        or (task.status == "awaiting_approval" and task.deadline_includes_approval)
    )
    return aware(now) + timedelta(seconds=remaining) if counts and remaining is not None else None
