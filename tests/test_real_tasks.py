# Git arguments in this local-only test are constructed from tmp_path and pinned literals.
# ruff: noqa: S603, S607

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from repopilot.real_tasks import (
    HiddenOverlayFile,
    export_git_tree,
    load_real_task_manifest,
    materialize_hidden_overlay,
    task_fingerprint,
)

PROJECT_ROOT = Path(__file__).parents[1]
MANIFEST = PROJECT_ROOT / "evals" / "real" / "manifest.json"


def test_manifest_has_twenty_pinned_real_cases_with_valid_fingerprints() -> None:
    manifest = load_real_task_manifest(MANIFEST)

    assert len(manifest.tasks) == 20
    assert len({task.id for task in manifest.tasks}) == 20
    assert {task.project for task in manifest.tasks} == {"thefuck"}
    assert {task.corpus.revision for task in manifest.tasks} == {
        "11c5f1eea954a42132cfd06bf257766a7963e0fd"
    }
    assert {task.verification.status for task in manifest.tasks} == {"assembled"}
    assert all(task.source.buggy_commit != task.source.fixed_commit for task in manifest.tasks)
    assert all(
        task.test.visible_argv
        == (
            "python",
            "-m",
            "pytest",
            "-q",
            "tests/rules/test_cd_mkdir.py",
        )
        for task in manifest.tasks
    )
    assert all(
        task.test.acceptance_argv[:3] == ("python", "-m", "pytest") for task in manifest.tasks
    )


def test_manifest_rejects_tampering_and_overlay_traversal(tmp_path: Path) -> None:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw["tasks"][0]["issue"]["body"] += " tampered"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        load_real_task_manifest(tampered)

    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw["tasks"][0]["test"]["hidden_overlay"][0]["source"] = "../reference.patch"
    raw["tasks"][0]["fingerprint"] = task_fingerprint(raw["tasks"][0])
    traversal = tmp_path / "traversal.json"
    traversal.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="contained relative path"):
        load_real_task_manifest(traversal)

    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    overlay = raw["tasks"][0]["test"]["hidden_overlay"][0]["destination"]
    raw["tasks"][0]["test"]["visible_argv"] = [
        "python",
        "-m",
        "pytest",
        overlay,
    ]
    raw["tasks"][0]["fingerprint"] = task_fingerprint(raw["tasks"][0])
    leaked_selector = tmp_path / "leaked-selector.json"
    leaked_selector.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="may not reference"):
        load_real_task_manifest(leaked_selector)


def test_git_export_contains_no_history_or_evaluator_artifacts(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Fixture"], check=True)
    (repository / "module.py").write_text("VALUE = 'buggy'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "module.py"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "buggy"], check=True)
    buggy = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "module.py").write_text("VALUE = 'fixed'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "commit", "-qam", "fixed"], check=True)
    fixed = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    workspace = export_git_tree(repository, buggy, tmp_path / "workspace")

    assert (workspace / "module.py").read_text(encoding="utf-8") == "VALUE = 'buggy'\n"
    assert not (workspace / ".git").exists()
    all_bytes = b"".join(path.read_bytes() for path in workspace.rglob("*") if path.is_file())
    assert fixed.encode() not in all_bytes

    with pytest.raises(ValueError, match="full lowercase"):
        export_git_tree(repository, "--output=outside.tar", tmp_path / "injected")


def test_hidden_overlay_is_verified_and_only_materialized_on_request(
    tmp_path: Path,
) -> None:
    task = load_real_task_manifest(MANIFEST).by_id("thefuck-032")
    destination = tmp_path / "workspace"
    target = destination / task.test.hidden_overlay[0].destination
    target.parent.mkdir(parents=True)
    target.write_text("agent-visible buggy test\n", encoding="utf-8")
    original_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    task = replace(
        task,
        test=replace(
            task.test,
            hidden_overlay=(replace(task.test.hidden_overlay[0], replaces_sha256=original_hash),),
        ),
    )

    written = materialize_hidden_overlay(task, MANIFEST.parent, destination)

    assert written == (target,)
    assert hashlib.sha256(target.read_bytes()).hexdigest() != original_hash
    assert hashlib.sha256(target.read_bytes()).hexdigest() == task.test.hidden_overlay[0].sha256


def test_hidden_overlay_rejects_checksum_mismatch_and_symlink_escape(
    tmp_path: Path,
) -> None:
    task = load_real_task_manifest(MANIFEST).by_id("thefuck-032")
    overlay = task.test.hidden_overlay[0]
    bad_task = replace(
        task,
        test=replace(
            task.test,
            hidden_overlay=(replace(overlay, sha256="0" * 64),),
        ),
    )
    destination = tmp_path / "workspace"
    target = destination / overlay.destination
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    bad_task = replace(
        bad_task,
        test=replace(
            bad_task.test,
            hidden_overlay=(
                replace(bad_task.test.hidden_overlay[0], replaces_sha256=current_hash),
            ),
        ),
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        materialize_hidden_overlay(bad_task, MANIFEST.parent, destination)

    outside = tmp_path / "outside"
    outside.mkdir()
    escaping = tmp_path / "escaping"
    escaping.mkdir()
    (escaping / "tests").symlink_to(outside, target_is_directory=True)
    shallow_overlay = HiddenOverlayFile(
        source=overlay.source,
        destination="tests/escaped.py",
        sha256=overlay.sha256,
        replaces_sha256=overlay.replaces_sha256,
    )
    escaping_task = replace(
        task,
        test=replace(task.test, hidden_overlay=(shallow_overlay,)),
    )
    with pytest.raises(ValueError, match="escapes workspace"):
        materialize_hidden_overlay(escaping_task, MANIFEST.parent, escaping)
