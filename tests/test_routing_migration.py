import asyncio

import pytest
from sqlalchemy import create_engine, text

from repopilot.db import Database
from repopilot.migrations import alembic_config
from repopilot.models import TaskCreate


@pytest.mark.asyncio
async def test_strategy_migration_preserves_legacy_rows_and_roundtrips_explicit_choice(tmp_path):
    from alembic import command

    url = f"sqlite+aiosqlite:///{tmp_path / 'routing.db'}"
    await asyncio.to_thread(command.upgrade, alembic_config(url), "20261022_0007")
    engine = create_engine(f"sqlite:///{tmp_path / 'routing.db'}")
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO tasks (id,status,repository_url,issue_title,issue_body,base_branch,
                test_command,max_iterations,graph_thread_id,created_at,updated_at)
            VALUES ('legacy','queued','demo://buggy-calculator','Fix add','Addition fails',
                'main','python -m pytest -q',2,'task-legacy',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
        """))
    engine.dispose()
    await asyncio.to_thread(command.upgrade, alembic_config(url), "head")
    database = Database(url)
    try:
        assert (await database.get_task("legacy")).execution_strategy == "workflow"
        saved = await database.create_task(TaskCreate(
            repository_url="demo://buggy-calculator", issue_title="Fix add",
            issue_body="Addition fails", execution_strategy="auto",
        ))
        assert (await database.get_task(saved.id)).execution_strategy == "auto"
    finally:
        await database.close()
