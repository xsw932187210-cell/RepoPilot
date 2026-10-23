"""A scripted, fixture-only Mock walkthrough. No configurable provider or repository."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from repopilot.candidates import ArtifactStore, Candidate, bound_resume
from repopilot.config import Settings
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.llm import MockAgentModel
from repopilot.models import CodeChangeOutput, FileEdit
from repopilot.policy_feedback import content_sha256
from repopilot.repository import WorkspaceManager
from repopilot.sandbox import LocalSandbox


class ScriptedDemoModel(MockAgentModel):
    """Intentionally keep the bug in proposal one to show a real failed test and retry."""

    def __init__(self) -> None:
        self.proposals = 0

    async def propose_changes(self, issue_title, issue_body, plan, context, reviewer_feedback):
        self.proposals += 1
        if self.proposals == 1:
            path = "calculator.py"
            before = context.files[path]
            return CodeChangeOutput(summary="Scripted first proposal keeps the bug", edits=[
                FileEdit(path=path, content="# Scripted demo proposal: still incorrect.\n" + before,
                         reason="Exercise the deterministic failing-test route",
                         expected_content_sha256=content_sha256(before)),
            ])
        return await super().propose_changes(
            issue_title, issue_body, plan, context, reviewer_feedback,
        )


def _fixture() -> Path:
    for root in (Path.cwd(), Path(__file__).resolve().parents[2]):
        fixture = root / "examples/buggy_calculator"
        if all((fixture / p).is_file() for p in ("calculator.py", "tests/test_calculator.py")):
            return fixture.resolve()
    raise ValueError("Run the demo from the repository root or the built image")


def _calls(state: dict) -> int:
    return sum(item["node"] in {"planner", "coder", "reviewer"}
               for item in state.get("node_metrics", []))


async def run_demo(
    decision: str = "inspect", progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], str]:
    if decision not in {"approve", "reject", "inspect"}:
        raise ValueError("Choose an explicit demo decision")
    tell = progress or (lambda message: None)
    fixture = _fixture()
    source_digest = hashlib.sha256((fixture / "calculator.py").read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="repopilot-demo-") as directory:
        settings = Settings(
            _env_file=None, model_provider="mock", review_model_provider="",
            model_name="scripted-demo-v1", openai_api_key="", openai_base_url="",
            google_cloud_api_key="", github_token="", api_key="", github_write_enabled=False,
            workspace_root=Path(directory) / "workspaces", demo_repository_root=fixture,
            sandbox_backend="local", model_max_calls=12,
        )
        deps = GraphDependencies(
            settings=settings, model=ScriptedDemoModel(), workspaces=WorkspaceManager(settings),
            sandbox=LocalSandbox(settings), publisher=GitHubPublisher(settings),
        )
        saver = InMemorySaver()
        graph = build_graph(deps, saver)
        task_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": task_id}}
        test_runs = []
        tell("创建内置计算器任务；全部角色为脚本化 Mock，真实 API 请求为 0。")
        async for update in graph.astream({
            "task_id": task_id, "repository_url": "demo://buggy-calculator",
            "issue_title": "Fix add returning the wrong result",
            "issue_body": "The add function subtracts instead of adding. Preserve subtract.",
            "base_branch": "main", "test_command": "python -m pytest -q",
            "execution_strategy": "workflow", "max_iterations": 2, "policy_retry_count": 0,
            "iteration": 0, "node_metrics": [], "reviewer_feedback": [],
        }, config, stream_mode="updates"):
            if "test_runner" in update:
                test = update["test_runner"]["test_result"]
                test_runs.append({"exit_code": test["exit_code"], "timed_out": test["timed_out"]})
                outcome = "通过" if test["exit_code"] == 0 else "失败"
                tell(f"第 {len(test_runs)} 轮测试：{outcome}。")
        checkpoint = await graph.aget_state(config)
        if checkpoint.next != ("approval",):
            raise RuntimeError("Scripted demo failed to reach the approval checkpoint")
        state = dict(checkpoint.values)
        candidate = Candidate.model_validate(state["candidate"])
        diff = ArtifactStore(settings.workspace_root / ".artifacts").read_diff(candidate).decode()
        binding = (candidate.candidate_id, state["test_evidence"]["evidence_digest"])
        calls_before = _calls(state)
        tell("已冻结候选与测试证据，图停在审批点；可查看保存的 candidate.diff。")
        if decision != "inspect":
            # This deliberately demonstrates only a recompiled graph in this same process.
            # Actual PostgreSQL + Worker process restart is a separate make verify proof.
            restored = build_graph(deps, saver)
            resumed_state = (await restored.aget_state(config)).values
            if binding != (resumed_state["candidate"]["candidate_id"],
                           resumed_state["test_evidence"]["evidence_digest"]):
                raise RuntimeError("Approval binding changed")
            tell("从同进程内存 checkpoint 重建图，提交命令参数指定的模拟审批决定。")
            state = await restored.ainvoke(Command(resume=bound_resume(
                state, approved=decision == "approve", feedback="Scripted offline demo decision",
            )), config)
        node_runs = dict(Counter(item["node"] for item in state["node_metrics"]))
        binding_unchanged = binding == (state["candidate"]["candidate_id"],
                                        state["test_evidence"]["evidence_digest"])
        fixture_unchanged = source_digest == hashlib.sha256(
            (fixture / "calculator.py").read_bytes()
        ).hexdigest()
        report = {
            "schema_version": "offline-demo-v1", "provider": "scripted-mock",
            "task_id": task_id, "external_model_requests": 0, "decision": decision,
            "status": "awaiting_approval" if decision == "inspect" else state["status"],
            "test_runs": test_runs, "node_runs": node_runs,
            "role_calls_before_resume": calls_before, "role_calls_after_resume": _calls(state),
            "candidate_binding_unchanged": binding_unchanged,
            "resumed": decision != "inspect",
            "resume_scope": "same_process_in_memory_checkpoint",
            "worker_process_restart_verified_here": False,
            "publication_enabled": False, "pull_request_url": state.get("pull_request_url"),
            "fixture_unchanged": fixture_unchanged,
        }
        expected_status = {"approve": "completed", "reject": "cancelled",
                           "inspect": "awaiting_approval"}[decision]
        report["ok"] = (
            report["status"] == expected_status and binding_unchanged and fixture_unchanged
            and len(test_runs) == 2 and test_runs[0]["exit_code"] != 0
            and test_runs[1]["exit_code"] == 0 and node_runs["test_runner"] == 2
            and calls_before == _calls(state) and not report["pull_request_url"]
        )
        if decision == "inspect":
            tell("演示结束：awaiting_approval；本次只查看审批点，没有执行恢复。")
        else:
            tell(f"演示结束：{report['status']}；恢复没有新增角色调用或重复测试。")
        return report, diff


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision", choices=("approve", "reject", "inspect"), default="inspect")
    parser.add_argument("--output", type=Path, default=Path("reports/demo"))
    args = parser.parse_args(argv)
    report, diff = asyncio.run(run_demo(args.decision, print))
    destination = args.output / report["task_id"]
    destination.mkdir(parents=True, exist_ok=False)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (destination / "summary.json").write_text(rendered)
    (destination / "candidate.diff").write_text(diff)
    print(json.dumps({"ok": report["ok"], "status": report["status"], "output": str(destination)}))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
