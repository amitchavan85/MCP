"""Configuration from environment variables (or a .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiKeyEntry(BaseModel):
    """One MCP client allowed to call this server. Only the SHA-256 of the key is stored."""

    name: str
    key_sha256: str
    scopes: list[str] = Field(default_factory=lambda: ["jira:read"])


# <project>/.env (works for `pip install -e .` from any working directory), then ./.env (wins if both exist).
PROJECT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
ENV_FILES = (str(PROJECT_ENV_FILE), ".env")


def loaded_env_files() -> list[str]:
    return [str(Path(f).resolve()) for f in dict.fromkeys(ENV_FILES) if Path(f).is_file()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILES, env_file_encoding="utf-8-sig", extra="ignore")

    # --- Jira Cloud connection (server-side credentials, never exposed to the model) ---
    jira_base_url: str = "https://your-domain.atlassian.net"
    jira_email: str = ""
    jira_api_token: SecretStr = SecretStr("")
    jira_timeout_seconds: float = 20.0

    # --- Guard rails ---
    jira_allowed_projects: list[str] = Field(
        default_factory=list,
        description="Project keys the model may touch. Empty list = all projects the Jira account can see.",
    )
    read_only: bool = Field(True, description="When true, every write tool refuses to run.")
    max_search_results: int = 50
    rate_limit_per_minute: int = 120

    # --- MCP endpoint security ---
    mcp_api_keys: list[ApiKeyEntry] = Field(default_factory=list)
    mcp_issuer_url: str = "http://localhost:8000"
    mcp_resource_url: str = "http://localhost:8000/mcp"
    mcp_allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost:*", "127.0.0.1:*"])
    mcp_allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:*", "http://127.0.0.1:*"])

    # --- Hosting / logging ---
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("jira_allowed_projects")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [p.strip().upper() for p in v if p.strip()]

    @field_validator("jira_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
