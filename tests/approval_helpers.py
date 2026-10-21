from datetime import UTC, datetime
from pathlib import Path

from repopilot.approval_models import ApprovalRequest, TestEvidence, evidence_identity
from repopilot.approvals import ApprovalStore
from repopilot.candidates import ArtifactStore
from repopilot.config import Settings
from repopilot.db import Database
from repopilot.generations import GenerationCoordinator, SQLGenerationStore
from repopilot.models import FileEdit, SandboxResult, TaskCreate, TaskStatus
from repopilot.repository import WorkspaceManager


async def approval_case(database: Database, root: Path, *, ttl_seconds: int = 86_400):
    settings = Settings(
        _env_file=None,
        workspace_root=root,
        demo_repository_root=Path(__file__).parents[1] / "examples" / "buggy_calculator",
        github_write_enabled=False,
    )
    task = await database.create_task(
        TaskCreate(
            repository_url="demo://buggy-calculator",
            issue_title="Fix the add function",
            issue_body="Addition subtracts instead of adding.",
        )
    )
    running = await database.transition_task(
        task.id,
        expected_status=task.status,
        expected_version=task.state_version,
        status=TaskStatus.RUNNING,
    )
    manager = WorkspaceManager(settings)
    workspace = await manager.prepare(task.id, task.repository_url, task.base_branch)
    source = workspace / "calculator.py"
    before = source.read_text()
    coordinator = GenerationCoordinator(
        ArtifactStore(root / ".artifacts"), SQLGenerationStore(database), manager
    )
    baseline = await coordinator.initialize(
        task.id, workspace, await manager.base_commit(workspace)
    )
    candidate = await coordinator.apply(
        baseline, [FileEdit(
            path="calculator.py", content=before.replace("return a - b", "return a + b"),
            reason="fixture fix",
        )],
        expected_contents={"calculator.py": before}, base_repository=workspace,
    )
    # These store-level fixtures intentionally do not claim actual test execution.
    result = SandboxResult(command=["pytest"], exit_code=0, duration_ms=1, image_id="unit-fixture")
    evidence = TestEvidence(
        evidence_digest="",
        candidate_id=candidate.candidate_id,
        artifact_digest=candidate.artifact_digest,
        image_id=result.image_id,
        command=result.command,
        policy={"kind": "store-unit-fixture"},
        result=result,
        observed_at=datetime.now(UTC),
    )
    evidence = evidence.model_copy(update={"evidence_digest": evidence_identity(evidence)})
    await ApprovalStore(database).open_request(
        task.id,
        expected_version=running.state_version,
        candidate=candidate,
        evidence=evidence,
        summary={"review": {"risk_level": "medium", "summary": "Fixture risk"}},
        ttl_seconds=ttl_seconds,
    )
    return settings, await ApprovalStore(database).current(task.id)


def decision_request(view, *, approved: bool = True, feedback: str = "reviewed") -> ApprovalRequest:
    return ApprovalRequest(
        schema_version="approval-v2",
        approval_id=view.approval_id,
        request_version=view.request_version,
        candidate_id=view.candidate.candidate_id,
        evidence_digest=view.evidence.evidence_digest,
        approved=approved,
        feedback=feedback,
    )
