"""Build a bound approval request from GET /tasks/{id}/approval (stdin)."""

import json
import sys

view = json.load(sys.stdin)
assert view["schema_version"] == "approval-v2" and view["status"] == "waiting"
print(
    json.dumps(
        {
            "schema_version": "approval-v2",
            "approval_id": view["approval_id"],
            "request_version": view["request_version"],
            "candidate_id": view["candidate"]["candidate_id"],
            "evidence_digest": view["evidence"]["evidence_digest"],
            "approved": True,
            "feedback": sys.argv[1] if len(sys.argv) > 1 else "smoke approval",
        }
    )
)
