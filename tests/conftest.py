"""The full service verification must not silently pass with skipped probes."""

import os

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.getenv("REPOPILOT_VERIFY_NO_SKIPS") == "1" and exitstatus == 0:
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        skipped = reporter.stats.get("skipped", []) if reporter else []
        if skipped:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            reporter.write_sep("!", "Full service verification cannot contain skipped probes")
