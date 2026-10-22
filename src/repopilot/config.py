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
    api_key: str = Field(default="", repr=False)

    database_url: str = Field(default="sqlite+aiosqlite:///./repopilot.db", repr=False)
    langgraph_database_url: str = Field(default="", repr=False)
    redis_url: str = Field(default="redis://localhost:6379/0", repr=False)
    queue_name: str = "repopilot:jobs"

    model_provider: str = "mock"
    model_name: str = "gpt-4.1-mini"
    openai_api_key: str = Field(default="", repr=False)
    openai_base_url: str = Field(default="", repr=False)
    google_cloud_api_key: str = Field(default="", repr=False)
    review_model_provider: str = ""
    review_model_name: str = "gpt-6.1-sol"
    chatgpt_storage_dir: Path = Path(".local/chatgpt")
    chatgpt_max_response_bytes: int = Field(default=65_536, ge=1_024, le=1_000_000)
    model_temperature: float = 0.0
    model_max_calls: int = Field(default=12, ge=1, le=100)
    model_request_timeout_seconds: float = Field(default=90.0, gt=0, le=900)
    model_max_rate_limit_retries: int = Field(default=2, ge=0, le=10)
    model_max_transient_retries: int = Field(default=2, ge=0, le=10)
    model_retry_base_seconds: float = Field(default=1.0, ge=0, le=60)
    model_max_retry_wait_seconds: float = Field(default=30.0, ge=0, le=600)
    model_max_total_backoff_seconds: float = Field(default=60.0, ge=0, le=3_600)
    model_max_total_tokens: int = Field(default=0, ge=0)
    model_max_output_tokens: int = Field(default=4_096, ge=1, le=200_000)

    task_timeout_seconds: int = Field(default=900, ge=1, le=86_400)
    deadline_includes_queue: bool = False
    deadline_includes_approval: bool = False
    cancellation_poll_seconds: float = Field(default=0.1, ge=0.01, le=5)
    publication_reconcile_seconds: float = Field(default=10, gt=0, le=60)

    workspace_root: Path = Path("./workspaces")
    demo_repository_root: Path = Path("./examples/buggy_calculator")
    max_context_files: int = Field(default=12, ge=1, le=30)
    max_context_chars: int = Field(default=70_000, ge=10_000, le=500_000)
    max_file_bytes: int = Field(default=120_000, ge=1_000, le=500_000)
    max_iterations: int = Field(default=2, ge=1, le=5)

    sandbox_backend: str = "local"
    sandbox_image: str = "repopilot-sandbox:local"
    sandbox_timeout_seconds: int = Field(default=120, ge=5, le=900)
    sandbox_memory: str = "512m"
    running_in_container: bool = False

    github_write_enabled: bool = False
    github_token: str = Field(default="", repr=False)
    github_allowed_owners: str = "xsw932187210-cell"

    @property
    def allowed_github_owners(self) -> set[str]:
        return {
            item.strip().lower() for item in self.github_allowed_owners.split(",") if item.strip()
        }

    def ensure_directories(self) -> None:
        self.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.workspace_root.is_symlink():
            raise ValueError("Workspace storage cannot be a symlink")
        self.workspace_root.chmod(0o700)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
