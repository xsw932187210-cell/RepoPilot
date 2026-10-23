import importlib.util
import json
from pathlib import Path


def test_failed_run_cannot_export_a_successful_delivery_summary(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/verification_summary.py"
    spec = importlib.util.spec_from_file_location("verification_summary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "uv.lock").write_text("lock")
    report = tmp_path / "report"
    report.mkdir()
    (report / "cleanup.json").write_text('{"resources_removed":true}')
    (report / "integration.xml").write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="1">'
        '<testcase name="private-test-name"><system-out>private-output</system-out>'
        '</testcase></testsuite></testsuites>'
    )
    result = module.summarize(tmp_path, report)
    assert result["ok"] is False
    assert result["checks"]["runner_completed"] is False
    assert result["checks"]["full_test_matrix"] is False
    assert result["tests"]["skipped"] == 1
    assert "private-test-name" not in json.dumps(result)
    assert "private-output" not in json.dumps(result)
