"""Project permissions, experiment migration and owned Docker fixture coverage."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import docker
import pytest

from repopilot.capabilities import real_evaluation_policy
from repopilot.config import Settings
from repopilot.llm import MockAgentModel
from repopilot.models import CodeChangeOutput, FileEdit
from repopilot.policy_feedback import content_sha256
from repopilot.real_evaluation import (
    OfflineWorkspace,
    execute_candidate,
    project_arguments,
    protect_experiment_directory,
    reproduce,
    run_experiment,
)
from repopilot.real_tasks import (
    load_real_task_manifest,
    materialize_hidden_overlay,
    prepare_agent_workspace,
    task_fingerprint,
)
from repopilot.security import CapabilityAction, SecurityError

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "evals" / "real" / "manifest.json"
LEGACY = MANIFEST.with_name("manifest-v1.json")
FIXTURE = ROOT / "evals" / "fixtures" / "tinyledger"
SYNTHETIC = FIXTURE / "corpus" / "manifest.json"


def fixture_source(destination: Path) -> Path:
    prepare = runpy.run_path(str(FIXTURE / "prepare.py"))["prepare"]
    buggy, fixed = prepare(destination)
    task = load_real_task_manifest(SYNTHETIC).tasks[0]
    assert (buggy, fixed) == (task.source.buggy_commit, task.source.fixed_commit)
    return destination


def write_manifest(tmp_path: Path, raw: dict) -> Path:
    for corpus in (MANIFEST.parent, SYNTHETIC.parent):
        for assets in ("environments", "overlays"):
            shutil.copytree(corpus / assets, tmp_path / assets, dirs_exist_ok=True)
    for project in raw["projects"].values():
        project["fingerprint"] = task_fingerprint(project)
    for task in raw["tasks"]:
        task["fingerprint"] = task_fingerprint(task)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def current_synthetic_runtime(destination: Path, image_id: str, python_version: str) -> Path:
    """A new engineering-only identity; never overwrite the historical fixture manifest."""
    raw = json.loads(SYNTHETIC.read_text())
    assert set(raw["projects"]) == {"tinyledger"}
    assert all(task["corpus"]["kind"] == "synthetic" for task in raw["tasks"])
    shutil.copytree(SYNTHETIC.parent, destination)
    recipe = (ROOT / "Dockerfile.sandbox").read_bytes()
    recipe_path = destination / "environments/ch18-sandbox.Dockerfile"
    recipe_path.write_bytes(recipe)
    lock_sha = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    raw["projects"]["tinyledger"]["runtime"].update(
        image=image_id, python_version=python_version,
        base_image=json.loads((ROOT / "delivery/images.json").read_text())["python"],
        recipe="environments/ch18-sandbox.Dockerfile",
        recipe_sha256=hashlib.sha256(recipe).hexdigest(),
        preparation=f"CH-18 synthetic regression only; current locked sandbox; uv.lock={lock_sha}",
        install_argv=[["uv", "sync", "--locked", "--only-group", "sandbox"]],
    )
    raw["projects"]["tinyledger"]["fingerprint"] = task_fingerprint(
        raw["projects"]["tinyledger"]
    )
    for task in raw["tasks"]:
        task["python_version"] = python_version
        task["fingerprint"] = task_fingerprint(task)
    path = destination / "manifest.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_current_synthetic_runtime_keeps_historical_assets_and_has_a_new_identity(tmp_path):
    original = SYNTHETIC.read_bytes()
    historical = load_real_task_manifest(SYNTHETIC).tasks[0]
    manifest = current_synthetic_runtime(tmp_path / "corpus", "sha256:" + "a" * 64, "3.12")
    current = load_real_task_manifest(manifest).tasks[0]
    assert current.fingerprint != historical.fingerprint
    assert current.adapter.fingerprint != historical.adapter.fingerprint
    assert current.source == historical.source and current.test == historical.test
    assert current.adapter.source_roots == historical.adapter.source_roots
    assert current.adapter.readonly_paths == historical.adapter.readonly_paths
    assert current.adapter.runtime.image == "sha256:" + "a" * 64
    assert SYNTHETIC.read_bytes() == original


def test_v1_bytes_and_twenty_task_identities_are_retained_read_only():
    assert hashlib.sha256(LEGACY.read_bytes()).hexdigest() == (
        "6b54dc349ba8f493e9cfb2702bf6c227c6b5c3da781e3246620779fdc9984471"
    )
    with pytest.raises(ValueError, match="Unsupported real-task schema"):
        load_real_task_manifest(LEGACY)
    legacy = load_real_task_manifest(LEGACY, allow_legacy_read=True)
    current = load_real_task_manifest(MANIFEST)
    assert current.schema_version == 2 and legacy.read_only
    with pytest.raises(ValueError, match="read-only"):
        legacy.require_executable()
    assert [asdict(replace(task, adapter=None)) for task in current.tasks] == [
        asdict(task) for task in legacy.tasks
    ]
    old_policy = real_evaluation_policy(max_file_bytes=120_000)
    new_policy = current.adapter_for("thefuck").capability_policy(120_000)
    paths = {path for task in current.tasks for path in task.expected_fix_files} | {
        "tests/rules/test_example.py",
        "setup.py",
        "README.md",
        "thefuck/.env",
        "thefuck/credentials.py",
        "thefuck/rules/alternative_fix.py",
        "new_project/code.py",
    }
    for path in paths:
        for action in (CapabilityAction.READ, CapabilityAction.WRITE):
            assert new_policy.permits(action, path) == old_policy.permits(action, path)
        assert not new_policy.create.directories


@pytest.mark.parametrize("field", ["license_spdx", "license_file"])
def test_missing_source_license_is_rejected(tmp_path, field):
    raw = json.loads(MANIFEST.read_text())
    raw["tasks"][0]["source"].pop(field)
    with pytest.raises(ValueError, match="source.license"):
        load_real_task_manifest(write_manifest(tmp_path, raw))


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("project", "source_roots", ["../escape"]),
        ("project", "source_roots", ["src/./package"]),
        ("project", "source_roots", ["src\\package"]),
        ("project", "source_roots", ["src/*"]),
        ("project", "source_roots", [".git"]),
        ("project", "source_roots", ["src", "SRC"]),
        ("project", "readonly_paths", []),
        ("project", "test_roots", ["unprotected"]),
        ("project", "visible_test_policy", "acceptance-tests"),
        ("project", "acceptance_test_policy", "candidate-tests"),
        ("runtime", "image", "python:latest"),
        ("runtime", "base_image", "python:3.8-slim"),
        ("runtime", "recipe_sha256", "0" * 63),
        ("runtime", "recipe_sha256", "0" * 64),
        ("runtime", "recipe", "../Dockerfile"),
        ("runtime", "preparation", ""),
        ("runtime", "install_argv", []),
        ("runtime", "pythonpath", ["/outside"]),
        ("runtime", "pythonpath", ["src/../outside"]),
        ("runtime", "python_version", "3.8; echo injected"),
    ],
)
def test_invalid_adapter_authority_and_environment_are_rejected(tmp_path, section, field, value):
    raw = json.loads(MANIFEST.read_text())
    adapter = raw["projects"]["thefuck"]
    target = adapter if section == "project" else adapter["runtime"]
    target[field] = value
    with pytest.raises(ValueError):
        load_real_task_manifest(write_manifest(tmp_path, raw))


def test_runtime_is_required_and_adapter_tampering_fails(tmp_path):
    raw = json.loads(MANIFEST.read_text())
    raw["projects"]["thefuck"].pop("runtime")
    with pytest.raises(ValueError, match="runtime information"):
        load_real_task_manifest(write_manifest(tmp_path, raw))
    raw = json.loads(MANIFEST.read_text())
    raw["projects"]["thefuck"]["source_roots"] = ["broader"]
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="adapter fingerprint mismatch"):
        load_real_task_manifest(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "../outside"),
        ("project", "unknown"),
        ("python_version", "3.12"),
        ("expected_fix_files", ["tests/rules/test_example.py"]),
        ("expected_fix_files", ["another_project/answer.py"]),
    ],
)
def test_case_cannot_change_project_authority(tmp_path, field, value):
    raw = json.loads(MANIFEST.read_text())
    raw["tasks"][0][field] = value
    with pytest.raises(ValueError):
        load_real_task_manifest(write_manifest(tmp_path, raw))


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-m", "pytest", "tests"],
        ["python", "-m", "pytest", "../test_escape.py"],
        ["python", "-m", "pytest", "--junitxml=answer.xml", "tests/test_x.py"],
        ["python", "-m", "pytest", "-c", "candidate.ini"],
    ],
)
def test_test_selection_cannot_expand_or_override_the_evaluator(tmp_path, argv):
    raw = json.loads(MANIFEST.read_text())
    raw["tasks"][0]["test"]["visible_argv"] = argv
    with pytest.raises(ValueError):
        load_real_task_manifest(write_manifest(tmp_path, raw))


def test_explicit_project_bindings_and_legacy_output_protection(tmp_path):
    assert project_arguments(
        ["thefuck=/a", "tinyledger=/b"],
        ("thefuck", "tinyledger"),
        field="source",
        required=True,
    ) == {"thefuck": "/a", "tinyledger": "/b"}
    assert project_arguments(Path("/single"), ("thefuck",), field="source", required=True) == {
        "thefuck": "/single"
    }
    for values in (["/ambiguous"], ["thefuck=/a"], ["thefuck=/a", "thefuck=/b"]):
        with pytest.raises(ValueError):
            project_arguments(values, ("thefuck", "tinyledger"), field="source", required=True)
    old = tmp_path / "old"
    old.mkdir()
    record = old / "reproduction-summary.json"
    record.write_text('{"historical": true}')
    original = record.read_bytes()
    with pytest.raises(ValueError, match="read-only"):
        protect_experiment_directory(old, {"schema": 2})
    assert record.read_bytes() == original
    new = tmp_path / "new"
    protect_experiment_directory(new, {"schema": 2, "image": "first"})
    protect_experiment_directory(new, {"schema": 2, "image": "first"})
    with pytest.raises(ValueError, match="identity differs"):
        protect_experiment_directory(new, {"schema": 2, "image": "second"})
    private = tmp_path / "credential-bearing"
    with pytest.raises(SecurityError):
        protect_experiment_directory(private, {"model": "sk-proj-" + "a" * 80})
    assert not private.exists()


async def test_multi_project_runtime_binding_and_resume_do_not_mix_environments(
    tmp_path, monkeypatch
):
    raw = json.loads(MANIFEST.read_text())
    synthetic = json.loads(SYNTHETIC.read_text())
    # Separate synthetic image identity makes accidental reuse of one global image observable.
    synthetic["projects"]["tinyledger"]["runtime"]["image"] = "sha256:" + "b" * 64
    raw["projects"].update(synthetic["projects"])
    raw["tasks"] = [raw["tasks"][0], synthetic["tasks"][0]]
    path = write_manifest(tmp_path, raw)
    sources = {project: tmp_path / project for project in ("thefuck", "tinyledger")}
    for source in sources.values():
        source.mkdir()
    args = SimpleNamespace(
        manifest=path,
        output=tmp_path / "records",
        case=None,
        source=[f"{project}={source}" for project, source in sources.items()],
        image=None,
        model=None,
        max_calls=6,
        reproduce_only=True,
    )
    monkeypatch.setattr(
        "repopilot.real_evaluation.Settings",
        lambda **kwargs: Settings(_env_file=None, model_provider="mock", **kwargs),
    )
    monkeypatch.setattr("repopilot.real_evaluation.image_identity", lambda image: image)
    calls = []

    async def reproduce_bound(task, source, corpus, settings):
        del corpus
        calls.append((task.project, source, settings.sandbox_image))
        return {"verified": True, "task_fingerprint": task.fingerprint, "case_id": task.id}

    monkeypatch.setattr("repopilot.real_evaluation.reproduce", reproduce_bound)
    result = await run_experiment(args)
    assert result["selected"] == result["reproduction_verified"] == 2
    assert [(project, source) for project, source, _ in calls] == list(sources.items())
    assert calls[0][2] != calls[1][2]
    assert result["evaluator_identity"]["projects"]["tinyledger"]["pythonpath"] == [
        "/workspace/src",
        "/workspace",
    ]
    await run_experiment(args)
    assert len(calls) == 2  # Exact identity resumes the original reproduction evidence.
    marker = args.output / "experiment-identity.json"
    before = marker.read_bytes()
    raw["projects"]["thefuck"]["runtime"]["pythonpath"] = ["thefuck", "."]
    write_manifest(tmp_path, raw)
    with pytest.raises(ValueError, match="identity differs"):
        await run_experiment(args)
    assert marker.read_bytes() == before and len(calls) == 2


async def test_runtime_override_is_checked_before_any_experiment_write(tmp_path, monkeypatch):
    args = SimpleNamespace(
        manifest=MANIFEST,
        output=tmp_path / "records",
        case=["thefuck-001"],
        source=tmp_path,
        image="untrusted-image-alias",
        model=None,
        max_calls=6,
        reproduce_only=True,
    )
    monkeypatch.setattr(
        "repopilot.real_evaluation.Settings",
        lambda **kwargs: Settings(_env_file=None, model_provider="mock", **kwargs),
    )
    monkeypatch.setattr(
        "repopilot.real_evaluation.image_identity",
        lambda image: "sha256:" + "0" * 64 if image == args.image else image,
    )
    with pytest.raises(ValueError, match="differs from the pinned"):
        await run_experiment(args)
    assert not args.output.exists()


async def test_second_project_permissions_and_hidden_overlay_are_separate(tmp_path):
    task = load_real_task_manifest(SYNTHETIC).tasks[0]
    source = fixture_source(tmp_path / "source")
    manager = OfflineWorkspace(Settings(_env_file=None), task, source, tmp_path / "agent")
    workspace = await manager.prepare(task.id, "fixture://tinyledger", "benchmark")
    context = manager.inspect(workspace, task.issue.body, ["unused planner hints"])
    assert "src/tinyledger/totals.py" in context.editable_paths
    assert "src/tinyledger/read_only.py" not in context.editable_paths
    assert "withheld-fixture-sentinel" not in context.render()
    assert not (workspace / ".git").exists()
    original = (workspace / "src/tinyledger/totals.py").read_text()
    for denied in ("checks/test_hidden.py", "src/tinyledger/read_only.py", "src/new.py"):
        with pytest.raises(SecurityError):
            manager.apply_edits(workspace, [FileEdit(path=denied, content="pass\n")])
    assert (workspace / "src/tinyledger/totals.py").read_text() == original
    acceptance = prepare_agent_workspace(task, source, tmp_path / "acceptance")
    materialize_hidden_overlay(task, SYNTHETIC.parent, acceptance)
    assert "withheld-fixture-sentinel" in (acceptance / "checks/test_hidden.py").read_text()
    assert "withheld-fixture-sentinel" not in (workspace / "checks/test_hidden.py").read_text()
    assert task.corpus.kind == "synthetic"


async def test_missing_checkout_license_is_rejected_before_model_context(tmp_path):
    task = load_real_task_manifest(SYNTHETIC).tasks[0]
    source = fixture_source(tmp_path / "source")
    task = replace(task, source=replace(task.source, license_file="ABSENT-LICENSE"))
    with pytest.raises(ValueError, match="license file"):
        prepare_agent_workspace(task, source, tmp_path / "agent")


class FixtureModel(MockAgentModel):
    """An explicit deterministic fixture strategy, with every model input captured."""

    def __init__(self):
        self.inputs: list[str] = []
        self.roles: list[str] = []

    async def plan(self, title, body):
        self.roles.append("planner")
        self.inputs.append(json.dumps([title, body]))
        return await super().plan(title, body)

    async def propose_changes(self, title, body, plan, context, feedback):
        self.roles.append("coder")
        self.inputs.append(json.dumps([title, body, plan.model_dump(), asdict(context), feedback]))
        path = "src/tinyledger/totals.py"
        before = context.files[path]
        return CodeChangeOutput(
            summary="Include negative amounts",
            edits=[
                FileEdit(
                    path=path,
                    content=before.replace(
                        "sum(amount for amount in amounts if amount > 0)", "sum(amounts)"
                    ),
                    expected_content_sha256=content_sha256(before),
                )
            ],
        )

    async def review(self, title, diff, result):
        self.roles.append("reviewer")
        self.inputs.append(json.dumps([title, diff, result.model_dump()]))
        return await super().review(title, diff, result)


@pytest.mark.skipif(
    os.getenv("REPOPILOT_CH07A_DOCKER_TEST") != "1",
    reason="dedicated CH-07A Docker fixture acceptance is not enabled",
)
@pytest.mark.parametrize("mode", ["oneshot", "workflow"])
async def test_second_project_full_docker_generation_and_hidden_acceptance(tmp_path, mode):
    manifest = SYNTHETIC
    if os.getenv("REPOPILOT_CH18_ADAPTER_FIXTURE") == "1":
        with docker.from_env() as client:
            image = client.images.get(Settings(_env_file=None).sandbox_image)
            environment = dict(item.split("=", 1) for item in image.attrs["Config"]["Env"])
        python_version = ".".join(environment["PYTHON_VERSION"].split(".")[:2])
        manifest = current_synthetic_runtime(tmp_path / "corpus", image.id, python_version)
    task = load_real_task_manifest(manifest).tasks[0]
    print(json.dumps({"synthetic_fixture_runtime": {
        "image_id": task.adapter.runtime.image, "python": task.adapter.runtime.python_version,
        "adapter_fingerprint": task.adapter.fingerprint, "task_fingerprint": task.fingerprint,
    }}))
    source = fixture_source(tmp_path / "source")
    settings = Settings(
        _env_file=None,
        sandbox_backend="docker",
        sandbox_image=task.adapter.runtime.image,
        github_write_enabled=False,
        model_provider="mock",
    )
    reproduction = await reproduce(task, source, manifest.parent, settings)
    assert reproduction["verified"]
    model = FixtureModel()
    result = await execute_candidate(
        task, mode, source, manifest.parent, settings, reproduction, model
    )
    assert result["resolved"] and result["acceptance"]["exit_code"] == 0
    assert result["comparison"]["fail_to_pass_count"] == 2
    assert result["comparison"]["pass_to_pass_count"] == 1
    assert result["changed_files"] == ["src/tinyledger/totals.py"]
    assert model.roles == (["coder"] if mode == "oneshot" else ["planner", "coder", "reviewer"])
    if mode == "workflow":
        assert result["workflow_status"] == "awaiting_approval"
        assert result["workflow_gate_passed"]
    all_inputs = "\n".join(model.inputs)
    for hidden in (
        task.source.fixed_commit,
        task.test.hidden_overlay[0].sha256,
        "withheld-fixture-sentinel",
        "test_refunds_are_subtracted",
        "test_only_refunds",
        "expected_fix_files",
        "hidden_overlay",
        "acceptance_argv",
    ):
        assert hidden not in all_inputs
