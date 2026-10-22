"""Opt-in synthetic model checks with a durable, shared attempt ceiling.

No model-generated code is executed. This is connection evidence, not a repair eval.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
from pathlib import Path

from repopilot.chatgpt_auth import PrivateStore, safe_status
from repopilot.config import Settings
from repopilot.data_boundary import configure_public_logging, safe_error
from repopilot.llm import build_agent_model, model_role_profile
from repopilot.model_calls import ModelCallBudget
from repopilot.models import CodeChangeOutput, PlanOutput, SandboxResult
from repopilot.repository import RepositoryContext


async def run_checks(settings: Settings, store: PrivateStore, max_calls: int) -> dict:
    identity = {
        "kind": "synthetic-role-connections-v1", "roles": model_role_profile(settings),
        "max_calls": max_calls, "request_timeout_seconds": settings.model_request_timeout_seconds,
        "automatic_retries": 0,
    }
    record = store.read("checks.json")
    if record and record.get("identity") != identity:
        raise ValueError("Connection-check configuration differs from the saved budget")
    record.setdefault("identity", identity)
    record.setdefault("checks", {})

    async def persist(snapshot):
        record["calls"] = dict(snapshot)
        store.write("checks.json", record)

    budget = ModelCallBudget(
        max_calls=max_calls, max_rate_limit_retries=0, max_transient_retries=0,
        max_output_tokens=settings.model_max_output_tokens,
        request_timeout_seconds=settings.model_request_timeout_seconds,
        initial_snapshot=record.get("calls"), state_hook=persist,
    )
    await budget.reconcile_incomplete()
    await persist(budget.snapshot())
    model = build_agent_model(settings, budget=budget)
    source = "def add(a, b):\n    return a - b\n"
    context = RepositoryContext(["calc.py"], {"calc.py": source}, ("calc.py",))
    evidence = SandboxResult(
        command=["synthetic-no-tests-run"], exit_code=1, duration_ms=0,
        stderr="Connection check only. No code or tests were executed.",
    )
    for stage in ("reviewer_only", "planner", "coder", "reviewer"):
        if record["checks"].get(stage, {}).get("status") == "succeeded":
            continue
        try:
            if stage == "reviewer_only":
                result = await model.review("Fix addition", "-a-b\n+a+b", evidence)
            elif stage == "planner":
                result = await model.plan(
                    "Fix addition", "Synthetic fixture: add(2, 3) should be 5.",
                )
                record["plan"] = result.model_dump()
            elif stage == "coder":
                result = await model.propose_changes(
                    "Fix addition", "Synthetic fixture: return a + b from add(a, b).",
                    PlanOutput.model_validate(record["plan"]), context, [],
                )
                record["change"] = result.model_dump()
            else:
                change = CodeChangeOutput.model_validate(record["change"])
                if any(edit.path != "calc.py" for edit in change.edits):
                    raise ValueError("Synthetic model requested an unexpected path")
                after = change.edits[0].content if change.edits else source
                diff = "".join(difflib.unified_diff(
                    source.splitlines(keepends=True), after.splitlines(keepends=True),
                    fromfile="calc.py", tofile="calc.py",
                ))
                result = await model.review("Fix addition", diff, evidence)
            record["checks"][stage] = {"status": "succeeded", "schema": type(result).__name__}
            if stage in {"reviewer", "reviewer_only"}:
                record["checks"][stage]["missing_test_evidence_rejected"] = not result.approved
        except Exception as error:
            record["checks"][stage] = {"status": "failed", "error": safe_error(error)}
            await persist(budget.snapshot())
            break
        await persist(budget.snapshot())
    # Never print model text or credentials. The local store is private and ignored.
    return {"identity": identity, "checks": record["checks"], "calls": record["calls"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    configure_public_logging()
    settings = Settings()
    if not args.run:
        session = PrivateStore(settings.chatgpt_storage_dir).read("session.json")
        print(json.dumps({"roles": model_role_profile(settings), "chatgpt": safe_status(session)}))
        return
    if args.max_calls is None or not 1 <= args.max_calls <= 20:
        parser.error("--run requires an explicitly approved --max-calls between 1 and 20")
    if settings.model_provider != "google_cloud" or settings.review_model_provider != "chatgpt":
        parser.error("Checks require google_cloud primary and chatgpt Reviewer")
    store = PrivateStore(Path(".local/model-connection-checks"))
    try:
        with store.lock():
            result = asyncio.run(run_checks(settings, store, args.max_calls))
        print(json.dumps(result, indent=2))
        if len(result["checks"]) != 4 or any(
            value["status"] != "succeeded" for value in result["checks"].values()
        ):
            raise SystemExit(1)
    except Exception as error:
        print(json.dumps({"error": safe_error(error)}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
