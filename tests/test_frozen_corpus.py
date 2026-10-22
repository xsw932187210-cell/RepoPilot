import hashlib
import json
import shutil
from pathlib import Path

import pytest

from repopilot.frozen_corpus import load_frozen_corpus
from repopilot.real_tasks import task_fingerprint

CORPUS = Path(__file__).parents[1] / "evals/real"


@pytest.fixture
def frozen_copy(tmp_path):
    corpus = tmp_path / "corpus"
    shutil.copytree(CORPUS, corpus)
    return corpus / "freeze-pilot-v1.json"


def rewrite(path, raw):
    raw["fingerprint"] = task_fingerprint(raw)
    path.write_text(json.dumps(raw))


def test_frozen_pilot_is_project_disjoint_and_preserves_historical_cases():
    frozen = load_frozen_corpus(CORPUS / "freeze-pilot-v1.json")
    old = json.loads((CORPUS / "manifest.json").read_text())
    pilot = json.loads(frozen.manifest_path.read_text())
    assert pilot["tasks"][:20] == old["tasks"]
    assert pilot["projects"]["thefuck"] == old["projects"]["thefuck"]
    assert len(frozen.select("development")) == 20
    assert len(frozen.select("holdout")) == 3
    assert {task.project for task in frozen.select("holdout")} == {"tqdm", "cookiecutter"}
    assert not frozen.exposed_holdout
    with pytest.raises(ValueError, match="explicit"):
        frozen.select("all")


@pytest.mark.parametrize("asset", [
    "manifest-pilot-v1.json", "provenance/pilot-v1.json", "exclusions-pilot-v1.json",
    "reproduction/pilot-v1.json", "licenses/tqdm-003.txt",
    "reproduction-checks/cookiecutter-004.py",
])
def test_frozen_asset_mutation_is_rejected(frozen_copy, asset):
    with (frozen_copy.parent / asset).open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="checksum"):
        load_frozen_corpus(frozen_copy)


@pytest.mark.parametrize("fault", ["overlap", "missing", "unknown", "project", "cross_split"])
def test_bad_partition_boundaries_are_rejected(frozen_copy, fault):
    raw = json.loads(frozen_copy.read_text())
    if fault == "overlap":
        raw["groups"][1]["task_ids"].append("tqdm-003")
    elif fault == "missing":
        raw["groups"][0]["task_ids"].pop()
    elif fault == "unknown":
        raw["groups"][1]["task_ids"].append("missing-case")
    elif fault == "project":
        raw["groups"][1]["project"] = "cookiecutter"
    else:
        raw["groups"][2]["task_ids"].remove("cookiecutter-004")
        raw["groups"].append({
            "id": "cookiecutter-other", "project": "cookiecutter",
            "partition": "development", "task_ids": ["cookiecutter-004"], "reason": "test",
        })
    rewrite(frozen_copy, raw)
    with pytest.raises(ValueError):
        load_frozen_corpus(frozen_copy)


@pytest.mark.parametrize("field,value", [
    ("verified", False), ("task_fingerprint", "0" * 64),
    ("adapter_fingerprint", "0" * 64), ("image_id", "sha256:" + "0" * 64),
    ("buggy_exit_code", 2), ("fixed_exit_code", 1), ("fail_to_pass_count", 0),
])
def test_wrong_reproduction_cannot_be_frozen(frozen_copy, field, value):
    raw = json.loads(frozen_copy.read_text())
    path = frozen_copy.parent / raw["reproduction"]
    evidence = json.loads(path.read_text())
    evidence["cases"][-1][field] = value
    path.write_text(json.dumps(evidence))
    raw["assets"][raw["reproduction"]] = hashlib.sha256(path.read_bytes()).hexdigest()
    rewrite(frozen_copy, raw)
    with pytest.raises(ValueError, match="Reproduction"):
        load_frozen_corpus(frozen_copy)


@pytest.mark.parametrize("kind", ["reference_example", "prompt_tuning", "router_tuning"])
def test_holdout_exposure_requires_a_new_freeze(frozen_copy, kind):
    raw = json.loads(frozen_copy.read_text())
    ledger = frozen_copy.parent / raw["contamination_ledger"]
    data = json.loads(ledger.read_text())
    before = load_frozen_corpus(frozen_copy)
    data["events"].append({
        "kind": kind, "recorded_at": "2026-10-24", "task_ids": ["tqdm-003"],
        "details": "Used reference feedback for tuning.",
    })
    ledger.write_text(json.dumps(data))
    after = load_frozen_corpus(frozen_copy)
    assert before.contamination_sha256 != after.contamination_sha256
    assert len(after.select("development")) == 20
    with pytest.raises(ValueError, match="exposed"):
        after.select("holdout")


def test_contamination_cannot_silently_name_an_unknown_case(frozen_copy):
    ledger = frozen_copy.parent / "contamination-pilot-v1.json"
    data = json.loads(ledger.read_text())
    data["events"][0]["task_ids"].append("unknown")
    ledger.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="known cases"):
        load_frozen_corpus(frozen_copy)


def test_freeze_identity_and_containment(frozen_copy):
    raw = json.loads(frozen_copy.read_text())
    raw["dataset_id"] = "changed"
    frozen_copy.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="modified"):
        load_frozen_corpus(frozen_copy)
    raw["manifest"] = "../manifest.json"
    raw["assets"]["../manifest.json"] = "0" * 64
    rewrite(frozen_copy, raw)
    with pytest.raises(ValueError, match="contained"):
        load_frozen_corpus(frozen_copy)
