import pytest
from pydantic import ValidationError

from repopilot.models import TaskCreate


def test_task_contract_bounds_iterations() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            repository_url="demo://buggy-calculator",
            issue_title="A valid title",
            issue_body="A valid body",
            max_iterations=20,
        )
