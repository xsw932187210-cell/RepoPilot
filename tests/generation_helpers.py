from pathlib import Path

from repopilot.candidates import ArtifactStore
from repopilot.config import Settings
from repopilot.db import Database
from repopilot.generations import GenerationCoordinator, MemoryGenerationStore, SQLGenerationStore
from repopilot.models import FileEdit, TaskCreate, TaskStatus
from repopilot.repository import WorkspaceManager


async def generation_case(root: Path, database: Database | None = None):
    settings = Settings(
        _env_file=None,
        workspace_root=root,
        demo_repository_root=Path(__file__).parents[1] / "examples" / "buggy_calculator",
        github_write_enabled=False,
    )
    task_id = "generation-case"
    if database:
        task = await database.create_task(
            TaskCreate(
                repository_url="demo://buggy-calculator",
                issue_title="Fix calculator",
                issue_body="Fix add and preserve complete source generations.",
            )
        )
        task_id = task.id
        await database.transition_task(
            task.id,
            expected_status=task.status,
            expected_version=task.state_version,
            status=TaskStatus.RUNNING,
        )
    manager = WorkspaceManager(settings)
    workspace = await manager.prepare(task_id, "demo://buggy-calculator", "main")
    # A second ordinary source file makes partial multi-file writes observable.
    (workspace / "helper.py").write_text("VALUE = 0\n")
    artifacts = ArtifactStore(root / ".artifacts")
    store = SQLGenerationStore(database) if database else MemoryGenerationStore()
    coordinator = GenerationCoordinator(artifacts, store, manager)
    parent = await coordinator.initialize(task_id, workspace, await manager.base_commit(workspace))
    with artifacts.materialize(parent) as snapshot:
        context = {path: (snapshot / path).read_text() for path in ("calculator.py", "helper.py")}
    edits = [
        FileEdit(
            path="calculator.py",
            content=context["calculator.py"].replace("return a - b", "return a + b"),
            reason="fix addition",
        ),
        FileEdit(path="helper.py", content="VALUE = 1\n", reason="second file"),
    ]
    return coordinator, parent, workspace, context, edits


def pair(artifacts, candidate):
    with artifacts.materialize(candidate) as snapshot:
        return (
            "return a + b" in (snapshot / "calculator.py").read_text(),
            (snapshot / "helper.py").read_text(),
        )
