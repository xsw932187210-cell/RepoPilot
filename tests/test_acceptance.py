import pytest
from docker.errors import NotFound

from repopilot.acceptance import AcceptanceRunner, compare_acceptance, parse_junit
from repopilot.config import Settings


def test_parse_independent_outcomes():
    result = parse_junit(
        b'<testsuite><testcase classname="a" name="ok"/>'
        b'<testcase classname="a" name="bad"><failure/></testcase></testsuite>'
    )
    assert result == {"a::ok": "passed", "a::bad": "failed"}


@pytest.mark.parametrize(
    "data",
    [
        b"<testsuite/>",
        b"<!DOCTYPE x><testsuite/>",
        b'<testsuite><testcase name="x"/><testcase name="x"/></testsuite>',
    ],
)
def test_reject_invalid_reports(data):
    with pytest.raises(ValueError):
        parse_junit(data)


def test_regression_cannot_be_hidden_by_fix():
    buggy = {"target": "failed", "existing": "passed"}
    fixed = {"target": "passed", "existing": "passed"}
    result = compare_acceptance(buggy, fixed, {"target": "passed", "existing": "failed"})
    assert not result["resolved"]
    assert result["regressions"] == ["existing"]
    assert compare_acceptance(buggy, fixed, fixed)["resolved"]
    assert not compare_acceptance(buggy, fixed, {"target": "passed"})["resolved"]


def test_non_reproducing_case_never_resolves():
    assert not compare_acceptance({"x": "passed"}, {"x": "passed"}, {"x": "passed"})["resolved"]


class MissingReportContainer:
    def __init__(self) -> None:
        self.removed = False

    def put_archive(self, path, snapshot):
        del snapshot
        return path == "/"

    def start(self):
        return None

    def wait(self, timeout):
        assert timeout > 0
        return {"StatusCode": 2}

    def logs(self):
        return b"pytest stopped before serializing junit"

    def get_archive(self, path):
        raise NotFound(f"missing {path}")

    def remove(self, *, force):
        self.removed = force


class MissingReportClient:
    def __init__(self, container):
        self.container = container
        self.containers = self
        self.closed = False

    def create(self, **kwargs):
        del kwargs
        return self.container

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_missing_junit_is_a_bounded_candidate_failure(tmp_path, monkeypatch):
    container = MissingReportContainer()
    client = MissingReportClient(container)
    monkeypatch.setattr("repopilot.acceptance.docker.from_env", lambda **_: client)

    result = await AcceptanceRunner(Settings(_env_file=None)).run(
        tmp_path, "python -m pytest -q tests/test_example.py"
    )

    assert result["exit_code"] == 2
    assert result["outcomes"] == {}
    assert result["report_missing"] is True
    assert "stopped before" in result["logs"]
    assert container.removed and client.closed
