import json
from pathlib import Path

from repopilot import doctor


def _root(tmp_path: Path) -> Path:
    fixture = tmp_path / "examples/buggy_calculator"
    fixture.mkdir(parents=True)
    (fixture / "calculator.py").write_text("")
    (tmp_path / "uv.lock").write_text('[[package]]\nname="sample"\nversion="1"\n')
    return tmp_path


def test_offline_preflight_uses_no_docker_or_provider_and_does_not_print_secrets(
    tmp_path, monkeypatch,
):
    root = _root(tmp_path)
    (root / ".env").write_text("GOOGLE_CLOUD_API_KEY=synthetic-private-value\n")
    calls = []
    monkeypatch.setattr(doctor, "_dependencies_match", lambda lock: True)
    monkeypatch.setattr(doctor, "_command", lambda argv: calls.append(argv) or True)
    result = doctor.inspect_environment(root)
    assert result["ok"]
    assert "synthetic-private-value" not in json.dumps(result)
    assert result["credential_fields_present"]["GOOGLE_CLOUD_API_KEY"] is True
    assert all(argv[0] == "uv" and "--offline" in argv for argv in calls)
    assert result["model_requests_issued"] == 0


def test_missing_images_and_daemon_are_failures_not_skipped_success(tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / "delivery").mkdir()
    (root / "delivery/images.json").write_text(json.dumps({
        name: "sample@sha256:" + "a" * 64 for name in ("python", "postgres", "redis")
    }))
    monkeypatch.setattr(doctor, "_dependencies_match", lambda lock: True)
    monkeypatch.setattr(doctor, "_command", lambda argv: argv[0] == "uv")
    result = doctor.inspect_environment(root, "docker")
    assert not result["ok"]
    assert {c["name"] for c in result["checks"] if c["status"] == "fail"} == {
        "docker_daemon", "compose", "image_python", "image_postgres", "image_redis",
    }


def test_invalid_config_does_not_echo_validation_input(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.delenv("MODEL_MAX_CALLS", raising=False)
    (root / ".env").write_text("MODEL_MAX_CALLS=synthetic-private-invalid-value\n")
    monkeypatch.setattr(doctor, "_dependencies_match", lambda lock: True)
    monkeypatch.setattr(doctor, "_command", lambda argv: True)
    result = doctor.inspect_environment(root)
    assert not result["ok"]
    assert "synthetic-private-invalid-value" not in json.dumps(result)
    assert {c["name"] for c in result["checks"] if c["status"] == "fail"} == {"configuration"}


def test_missing_or_stale_lock_is_not_ready(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(doctor, "_dependencies_match", lambda lock: True)
    monkeypatch.setattr(doctor, "_command", lambda argv: False)
    assert not doctor.inspect_environment(root)["ok"]
    (root / "uv.lock").unlink()
    assert not doctor.inspect_environment(root)["ok"]


def test_dependency_drift_is_rejected(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(doctor.metadata, "requires", lambda name: ["sample>=1"])
    monkeypatch.setattr(doctor.metadata, "distributions", lambda: [
        SimpleNamespace(metadata={"Name": "sample"}, version="2"),
    ])
    assert not doctor._dependencies_match({"package": [{"name": "sample", "version": "1"}]})
