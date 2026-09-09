"""Non-destructive runtime probes for the configured Linux test container."""

import os
import socket
from pathlib import Path

import pytest


def test_unprivileged_identity():
    assert os.getuid() == 10001


def test_no_host_control_files():
    assert not Path("/var/run/docker.sock").exists()
    assert not Path("/app/.env").exists()
    assert not os.environ.get("OPENAI_API_KEY")


def test_capabilities_and_privilege_escalation_disabled():
    status = Path("/proc/self/status").read_text()
    values = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    assert int(values["CapEff"].strip(), 16) == 0
    assert values["NoNewPrivs"].strip() == "1"


def test_no_external_route():
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    assert not any(line.split()[1] == "00000000" for line in routes)
    with socket.socket() as connection:
        connection.settimeout(0.2)
        with pytest.raises(OSError):
            connection.connect(("192.0.2.1", 80))
