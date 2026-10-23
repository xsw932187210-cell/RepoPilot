"""Read-only local preflight. No model, login, pull, build or service mutation."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from repopilot.config import Settings


def _command(argv: list[str]) -> bool:
    try:
        result = subprocess.run(  # noqa: S603 -- fixed operator tools; no shell or raw output.
            argv, capture_output=True, timeout=15, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _dependencies_match(lock: dict) -> bool:
    try:
        versions: dict[str, set[str]] = {}
        for package in lock["package"]:
            versions.setdefault(package["name"], set()).add(package["version"])
        names = {
            re.split(r"[\[<>=!~; ]", requirement)[0].lower().replace("_", "-")
            for requirement in metadata.requires("repopilot") or [] if "extra ==" not in requirement
        }
        installed = {d.metadata["Name"].lower().replace("_", "-"): d.version
                     for d in metadata.distributions()}
        matching = all(installed.get(name) in versions.get(name, set()) for name in names)
        matching &= all(version in versions[name] for name, version in installed.items()
                        if name in versions and name != "repopilot")
        return bool(names) and matching
    except (metadata.PackageNotFoundError, KeyError, TypeError):
        return False


def inspect_environment(root: Path, profile: str = "offline") -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, reason: str) -> None:
        checks.append({"name": name, "status": "pass" if passed else "fail", "reason": reason})

    add("python", (3, 12) <= sys.version_info[:2] < (3, 14), "python_3_12_or_3_13_required")
    lock_path = root / "uv.lock"
    lock = None
    try:
        lock = tomllib.loads(lock_path.read_text())
        if not isinstance(lock.get("package"), list) or not lock["package"]:
            raise ValueError("Invalid lockfile")
        add("lockfile", True, "lockfile_readable")
    except (OSError, ValueError):
        add("lockfile", False, "lockfile_missing_or_invalid")
    if lock:
        add("dependencies", _dependencies_match(lock), "installed_versions_match_lock")
        add("lock_current", _command([
            "uv", "lock", "--check", "--offline", "--python", sys.executable,
            "--no-python-downloads", "--project", str(root),
        ]), "uv_lock_check_required_no_resolution_or_network")
    try:
        settings = Settings(_env_file=root / ".env")
        add("configuration", True, "configuration_parsed_without_connecting")
        fields = {name.upper(): bool(getattr(settings, name)) for name in (
            "api_key", "openai_api_key", "google_cloud_api_key", "github_token",
        )}
    except Exception:  # No validation text: invalid values can contain credentials.
        add("configuration", False, "configuration_invalid")
        fields = {}
    add("mock_fixture", (root / "examples/buggy_calculator/calculator.py").is_file(),
        "bundled_mock_fixture_required")
    if profile in {"docker", "corpus"}:
        add("docker_daemon", _command(["docker", "info", "--format", "{{.ServerVersion}}"]),
            "docker_daemon_unavailable_if_failed")
        add("compose", _command(["docker", "compose", "version", "--short"]),
            "compose_v2_required")
        try:
            images = json.loads((root / "delivery/images.json").read_text())
            for name in ("python", "postgres", "redis"):
                image = images[name]
                valid = isinstance(image, str) and re.fullmatch(
                    r"[a-z0-9:./_-]+@sha256:[a-f0-9]{64}", image,
                )
                add(f"image_{name}", bool(valid) and _command(
                    ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                ), "pinned_image_not_cached_if_failed")
        except (OSError, ValueError, KeyError):
            add("images", False, "image_manifest_missing_or_invalid")
    if profile == "corpus":
        from repopilot.frozen_corpus import load_frozen_corpus

        try:
            corpus = load_frozen_corpus(root / "evals/real/freeze-pilot-v1.json")
            add("frozen_metadata", True, "frozen_metadata_hashes_valid")
            for project in sorted({task.project for task in corpus.manifest.tasks}):
                cache = root / "reports/real-corpus-cache" / project
                add(f"cache_{project}", cache.is_dir() and (cache / ".git").exists(),
                    "source_cache_missing_if_failed_not_a_reproduction_check")
        except Exception:
            add("frozen_metadata", False, "frozen_metadata_invalid")
    return {
        "schema_version": "delivery-doctor-v1", "profile": profile,
        "ok": all(check["status"] == "pass" for check in checks),
        "checks": checks, "credential_fields_present": fields,
        "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest() if lock else None,
        "model_requests_issued": 0,
        "scope": "local_readiness_only_not_service_or_provider_acceptance",
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--profile", choices=("offline", "docker", "corpus"), default="offline")
    args = parser.parse_args(argv)
    result = inspect_environment(args.root.resolve(), args.profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
