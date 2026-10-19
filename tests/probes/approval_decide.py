"""Exit immediately after the PostgreSQL decision transaction commits."""

import asyncio
import os
import sys

from repopilot.approval_models import ApprovalRequest
from repopilot.approvals import ApprovalStore
from repopilot.config import Settings
from repopilot.db import Database


async def run():
    database = Database(Settings(_env_file=None).database_url)
    store = ApprovalStore(database)
    view = await store.current(sys.argv[1])
    await store.decide(
        view.task_id,
        ApprovalRequest(
            schema_version="approval-v2",
            approval_id=view.approval_id,
            request_version=view.request_version,
            candidate_id=view.candidate.candidate_id,
            evidence_digest=view.evidence.evidence_digest,
            approved=True,
            feedback="process commit probe",
        ),
        actor="process-probe",
    )
    os._exit(87)


asyncio.run(run())
