"""Opt-in frozen CH-01C protocol with one pre-existing local authorization.

The default command reads metadata only. --run uses the exact same bound output
and durable reservations on every resume; it never manufactures authorization.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from repopilot.chatgpt_auth import PrivateStore
from repopilot.config import Settings
from repopilot.data_boundary import configure_public_logging, require_secret_free, safe_error
from repopilot.frozen_corpus import load_frozen_corpus
from repopilot.llm import call_policy_from_settings, model_role_profile
from repopilot.model_calls import EVALUATION_BUDGET_VERSION
from repopilot.real_evaluation import implementation_sha256, run_experiment
from repopilot.routing import RoutingPolicy


def digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def prepare_plan(protocol: dict, settings: Settings, root: Path) -> dict:
    require_secret_free(protocol)
    if protocol.get("protocol") not in {"ch01c-pilot-v1", "ch01c-pilot-v2"}:
        raise ValueError("unsupported_experiment_protocol")
    maximum = 108 if protocol["protocol"] == "ch01c-pilot-v1" else 78
    if (
        protocol.get("strategies") != ["oneshot", "workflow", "auto"]
        or protocol.get("repeats") != 2 or len(protocol.get("cases", [])) != 3
        or protocol.get("max_calls_per_unit") != 6
        or protocol.get("max_api_attempts") != maximum or protocol.get("order") != "rotating-v1"
        or protocol.get("partition") != "holdout" or protocol.get("provider_seed") is not None
    ):
        raise ValueError("experiment_outside_approved_design")
    frozen = load_frozen_corpus(root / protocol["freeze"])
    if [task.id for task in frozen.select("holdout")] != protocol["cases"]:
        raise ValueError("experiment_cases_differ_from_frozen_holdout")
    roles = model_role_profile(settings)
    for role, declared in (("coder", protocol["primary"]), ("reviewer", protocol["reviewer"])):
        if any(roles[role][key] != declared[key] for key in ("provider", "model")):
            raise ValueError("configured_model_differs_from_frozen_protocol")
    if (
        settings.model_temperature != protocol["temperature"]
        or settings.model_max_output_tokens != protocol["gemini_max_output_tokens"]
        or settings.chatgpt_max_response_bytes != protocol["chatgpt_local_max_stream_bytes"]
        or settings.model_request_timeout_seconds != protocol["request_timeout_seconds"]
        or asdict(RoutingPolicy.from_settings(settings)) != protocol["routing_policy"]
    ):
        raise ValueError("configured_limits_differ_from_frozen_protocol")
    if any(
        getattr(settings, key) != value for key, value in protocol["execution_limits"].items()
    ):
        raise ValueError("configured_execution_limits_differ_from_frozen_protocol")
    dependencies = {name: version(name) for name in protocol["sdk_versions"]}
    if dependencies != protocol["sdk_versions"]:
        raise ValueError("configured_sdk_versions_differ_from_frozen_protocol")
    output = (root / protocol["output"]).resolve()
    if not output.is_relative_to((root / "reports").resolve()):
        raise ValueError("experiment_output_outside_reports")
    implementation = implementation_sha256()
    call_policy = {
        **asdict(call_policy_from_settings(settings)), "version": EVALUATION_BUDGET_VERSION,
    }
    plan = {
        "protocol": protocol, "protocol_sha256": digest(protocol),
        "implementation_sha256": implementation,
        "source_frozen": protocol["implementation_sha256"] == implementation,
        "model_roles": roles, "sdk_versions": dependencies,
        "call_policy": call_policy,
        "orchestrator_python": sys.version.split()[0],
        "freeze_fingerprint": frozen.fingerprint,
        "contamination_sha256": frozen.contamination_sha256,
    }
    require_secret_free(plan)
    return {**plan, "fingerprint": digest(plan)}


class AuthorizedExperiment:
    """An existing operator grant binds one immutable plan and one output path."""

    def __init__(self, store: PrivateStore, plan: dict, output: Path):
        self.store, self.plan, self.output = store, plan, output.resolve()
        protocol = plan.get("protocol", {})
        self.maximum = protocol.get("max_api_attempts", 108)
        self.filename = protocol.get("authorization_file", "ch01c.json")
        if (self.maximum, self.filename) not in {
            (108, "ch01c.json"), (78, "ch01c-v2.json"),
        }:
            raise ValueError("unsupported_experiment_authorization")
        self.record = store.read(self.filename)
        required = {
            "task": "CH-01C", "max_api_attempts": self.maximum,
            "per_case_strategy_repeat_max_attempts": 6,
            "cases": 3, "repeats": 2, "includes_failures_and_retries": True,
        }
        if self.record.get("status") not in {
            "authorized_not_started", "running", "pending", "completed", "interrupted",
        } or not self.record.get("approved_at") or any(
            self.record.get(key) != value for key, value in required.items()
        ) or self.record.get("strategies") != ["oneshot", "workflow", "auto"] or set(
            self.record.get("providers", [])
        ) != {"google_cloud", "chatgpt"}:
            raise ValueError("existing_user_authorization_required")
        prior = None
        if self.maximum == 78:
            prior = store.read("ch01c.json")
            binding = prior.get("binding") or {}
            old_ids = prior.get("reservation_ids", [])
            if (
                len(old_ids) != 30 or prior.get("max_api_attempts") != 108
                or binding.get("fingerprint") != protocol.get("supersedes_plan_fingerprint")
                or self.record.get("prior_reserved_api_attempts") != 30
                or self.record.get("original_total_ceiling") != 108
            ):
                raise ValueError("corrected_run_requires_confirmed_remaining_budget")
            old_ledger = json.loads((
                Path(binding["output"]) / "model-records/api-reservations.json"
            ).read_text())
            if [item["attempt_id"] for item in old_ledger["reservations"]] != old_ids:
                raise ValueError("prior_run_reservation_history_changed")
        if not plan["source_frozen"]:
            raise ValueError("experiment_implementation_not_frozen")
        binding = {"fingerprint": plan["fingerprint"], "output": str(self.output)}
        if self.record.get("binding") and self.record["binding"] != binding:
            raise ValueError("authorization_already_bound_to_different_experiment")
        self.record["binding"] = binding
        self.record.setdefault("reservation_ids", [])
        ledger = self.output / "model-records/api-reservations.json"
        if ledger.exists():
            self._reconcile(json.loads(ledger.read_text()))
        elif self.record["reservation_ids"]:
            raise ValueError("authorized_experiment_ledger_missing")
        self.record["status"] = "running"
        if prior is not None:
            prior["status"] = "superseded"
            prior["superseded_by_authorization"] = self.filename
            self.store.write("ch01c.json", prior)
        self.store.write(self.filename, self.record)

    def _reconcile(self, budget: dict) -> None:
        reservations = budget.get("reservations")
        if (
            not isinstance(reservations, list) or len(reservations) > self.maximum
            or budget.get("max_api_attempts") != self.maximum
            or not isinstance(budget.get("fingerprint"), str)
        ):
            raise ValueError("invalid_authorized_reservation_ledger")
        ids = [item["attempt_id"] for item in reservations]
        previous = self.record["reservation_ids"]
        if (
            any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != len(ids) or ids[:len(previous)] != previous
        ):
            raise ValueError("authorized_reservation_history_lost_or_changed")
        old_fingerprint = self.record.get("cohort_fingerprint")
        if old_fingerprint and old_fingerprint != budget.get("fingerprint"):
            raise ValueError("authorized_cohort_identity_changed")
        self.record["reservation_ids"] = ids
        self.record["cohort_fingerprint"] = budget.get("fingerprint")
        self.record["reserved_api_attempts"] = len(ids)

    async def observe(self, budget: dict) -> None:
        self._reconcile(budget)
        self.store.write(self.filename, self.record)

    def finish(self, status: str) -> None:
        self.record["status"] = status
        self.store.write(self.filename, self.record)


async def execute_protocol(protocol: dict, sources: list[str], root: Path) -> dict:
    settings = Settings(
        sandbox_backend="docker", github_write_enabled=False,
        sandbox_timeout_seconds=120, max_context_files=12, max_context_chars=70_000,
        max_iterations=2,
        model_name=protocol["primary"]["model"],
        model_temperature=protocol["temperature"],
        model_max_calls=protocol["max_calls_per_unit"],
        model_max_output_tokens=protocol["gemini_max_output_tokens"],
    )
    plan = prepare_plan(protocol, settings, root)
    output = root / protocol["output"]
    store = PrivateStore(root / ".local/evaluation-authorization")
    with store.lock():
        authorization = AuthorizedExperiment(store, plan, output)
        arguments = argparse.Namespace(
            manifest=root / "evals/real/manifest-pilot-v1.json", freeze=root / protocol["freeze"],
            partition="holdout", case=protocol["cases"], output=output, source=sources,
            run_id=protocol["run_id"], repeats=2, strategy_version=protocol["strategy_version"],
            max_total_calls=protocol["max_api_attempts"], max_calls=6,
            modes=protocol["strategies"], image=None,
            model=protocol["primary"]["model"],
            max_output_tokens=protocol["gemini_max_output_tokens"],
            temperature=protocol["temperature"], reproduce_only=False,
            reservation_observer=authorization.observe,
            protocol_identity=plan,
        )
        try:
            result = await run_experiment(arguments)
            report = json.loads((output / "model-report.json").read_text())
            pending = any(r["status"].startswith("pending") for r in report["records"])
            authorization.finish("pending" if pending else "completed")
            return {**result, "authorized_budget": {
                "maximum": authorization.maximum,
                "prior_reserved": 30 if authorization.maximum == 78 else 0,
                "original_total_ceiling": 108,
                "reserved": len(authorization.record["reservation_ids"]),
                "status": authorization.record["status"],
            }}
        except BaseException:
            authorization.finish("interrupted")
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path,
                        default=Path("evals/real/experiments/ch01c-pilot-v2.json"))
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    configure_public_logging()
    try:
        protocol = json.loads(args.protocol.read_text())
        if args.run:
            if not args.source:
                parser.error("--run requires trusted --source PROJECT=PATH mappings")
            result = asyncio.run(execute_protocol(protocol, args.source, Path.cwd()))
        else:
            settings = Settings(
                sandbox_backend="docker", github_write_enabled=False,
                sandbox_timeout_seconds=120, max_context_files=12, max_context_chars=70_000,
                max_iterations=2,
                model_name=protocol["primary"]["model"],
                model_temperature=protocol["temperature"],
                model_max_calls=protocol["max_calls_per_unit"],
                model_max_output_tokens=protocol["gemini_max_output_tokens"],
            )
            result = prepare_plan(protocol, settings, Path.cwd())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({"error": safe_error(error)}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
