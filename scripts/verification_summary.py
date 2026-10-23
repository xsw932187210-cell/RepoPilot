#!/usr/bin/env python3
"""Export allowlisted local verification facts, excluding test names and logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def snapshot(root: Path) -> dict[str, str]:
    files = {p for directory in ("src", "tests", "scripts", "delivery", "requirements")
             for p in (root / directory).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts}
    files.update(root.glob("Dockerfile*"))
    files.update(root.glob("docker-compose*.yml"))
    files.update(root / name for name in ("pyproject.toml", "uv.lock", "Makefile", "README.md"))
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)}


def summarize(root: Path, report: Path) -> dict:
    def load(name: str):
        return json.loads((report / name).read_text())

    checks = {}
    facts = {}
    try:
        checks["runner_completed"] = load("completed.json")["completed"] is True
        checks["doctor"] = load("doctor.json")["ok"] is True
    except (OSError, ValueError, KeyError):
        checks["runner_completed"] = False
    try:
        suites = ET.parse(report / "integration.xml").getroot().iter("testsuite")  # noqa: S314
        tests = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
        for suite in suites:
            for key in tests:
                tests[key] += int(suite.get(key, "0"))
        facts["tests"] = tests
        checks["full_test_matrix"] = (
            tests["tests"] > 0 and tests["failures"] == tests["errors"] == tests["skipped"] == 0
        )
        coverage = ET.parse(report / "coverage.xml").getroot()  # noqa: S314
        facts["coverage_percent"] = round(float(coverage.attrib["line-rate"]) * 100, 2)
        checks["coverage_floor"] = facts["coverage_percent"] >= 70
        mock = load("mock-evaluation.json")["summary"]
        facts["mock"] = {key: mock[key] for key in ("count", "successful")}
        checks["mock"] = mock["count"] == mock["successful"] == 10
        source = load("source-hashes.json")
        installed = load("installed-source-hashes.json")
        api = load("api-source-hashes.json")
        source_package = {k: v for k, v in source.items() if k.startswith("src/")}
        checks["installed_source_identity"] = installed == api == source_package
        checks["inputs_unchanged_during_verification"] = load("input-files.json") == snapshot(root)
        facts["source_tree_sha256"] = hashlib.sha256(
            json.dumps(source, sort_keys=True).encode()
        ).hexdigest()
        for name in ("smoke.jsonl", "recovery.jsonl", "recovery-oneshot.jsonl"):
            checks[name.replace(".jsonl", "")] = (report / name).is_file() and bool(
                (report / name).read_text().strip()
            )
        facts["runtime_versions"] = load("runtime-versions.json")
    except (OSError, ValueError, KeyError, ET.ParseError):
        checks["required_evidence_complete"] = False
    try:
        checks["resources_removed"] = load("cleanup.json")["resources_removed"] is True
    except (OSError, ValueError, KeyError):
        checks["resources_removed"] = False
    commit = subprocess.run(  # noqa: S603,S607 -- fixed read-only Git command.
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False,  # noqa: S607
    ).stdout.strip()
    return {
        "schema_version": "delivery-verification-v1", "ok": all(checks.values()),
        "base_commit": commit, "checks": checks, **facts,
        "lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        "model_requests_issued": 0, "provider_quality_verified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--snapshot", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.snapshot:
        result, name = snapshot(root), "input-files.json"
    else:
        result, name = summarize(root, args.report), "delivery-summary.json"
    (args.report / name).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if not args.snapshot:
        print(json.dumps(result))
        if not result["ok"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
