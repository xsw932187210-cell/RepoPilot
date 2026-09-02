from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "RepoPilot"
    environment: str = "development"
    api_key: str = ""

    database_url: str = "sqlite+aiosqlite:///./repopilot.db"
    langgraph_database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "repopilot:jobs"

    model_provider: str = "mock"
    model_name: str = "gpt-4.1-mini"
    openai_api_key: str = ""
    openai_base_url: str = ""
    model_temperature: float = 0.0

    workspace_root: Path = Path("./workspaces")
    demo_repository_root: Path = Path("./examples/buggy_calculator")
    max_context_files: int = Field(default=12, ge=1, le=30)
    max_file_bytes: int = Field(default=120_000, ge=1_000, le=500_000)
    max_iterations: int = Field(default=2, ge=1, le=5)

    sandbox_backend: str = "local"
    sandbox_image: str = "repopilot-sandbox:local"
    sandbox_timeout_seconds: int = Field(default=120, ge=5, le=900)
    sandbox_memory: str = "512m"
    running_in_container: bool = False

    github_write_enabled: bool = False
    github_token: str = ""
    github_allowed_owners: str = "xsw932187210-cell"

    @property
    def allowed_github_owners(self) -> set[str]:
        return {
            item.strip().lower()
            for item in self.github_allowed_owners.split(",")
            if item.strip()
        }

    def ensure_directories(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
