"""Insert the pre-CH-06A task contract when testing an older Alembic head."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import text


async def insert_legacy_task(database, data):
    task_id = str(uuid.uuid4())
    values = {
        **data.model_dump(
            exclude={
                "execution_timeout_seconds",
                "deadline_includes_queue",
                "deadline_includes_approval",
            }
        ),
        "id": task_id,
        "graph_thread_id": f"task-{task_id}",
        "now": datetime.now(UTC),
    }
    async with database.engine.begin() as connection:
        await connection.execute(
            text("""
            INSERT INTO tasks (id,status,repository_url,issue_title,issue_body,base_branch,
                test_command,max_iterations,graph_thread_id,state_version,result_schema_version,
                created_at,updated_at)
            VALUES (:id,'queued',:repository_url,:issue_title,:issue_body,:base_branch,
                :test_command,:max_iterations,:graph_thread_id,1,1,:now,:now)
        """),
            values,
        )
    return SimpleNamespace(id=task_id, issue_title=data.issue_title)
