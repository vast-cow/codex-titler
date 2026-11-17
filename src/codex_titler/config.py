from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TITLER_", env_file=".env", extra="ignore")
    endpoint: str = "unix:///tmp/codex-app-server.sock"
    stdio_command: list[str] | None = None
    bearer_token: SecretStr | None = None
    database: Path = Path("titler.sqlite3")
    threshold: int = Field(120, ge=2)
    title_limit: int = Field(80, ge=1)
    debounce: float = Field(2, ge=0)
    reconcile_interval: float = Field(60, gt=0)
    concurrency: int = Field(2, ge=1)
    invalid_retries: int = Field(1, ge=0, le=5)
    context_retries: int = Field(2, ge=0, le=5)
    request_timeout: float = Field(60, gt=0)
    generation_timeout: float = Field(180, gt=0)
    read_retries: int = Field(3, ge=1)
    backoff: float = Field(1, gt=0)
    cooldown: float = Field(300, gt=0)
    queue_size: int = Field(1000, ge=1)
    max_message_bytes: int = Field(67108864, ge=1024)
    # Explicit operator override: the API does not advertise an effective window.
    context_limit: int = Field(..., ge=2048)
    auto_compact_limit: int | None = Field(None, ge=2048)
    output_reserve: int = Field(2048, ge=1)
    server_overhead: int = Field(4096, ge=0)
    safety_margin: int = Field(2048, ge=0)
    dry_run: bool = False
    log_level: str = "INFO"
    helper_config: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_limits(self):
        if self.title_limit >= self.threshold:
            raise ValueError("title_limit must be below threshold")
        if self.input_budget < 512:
            raise ValueError("context budget leaves fewer than 512 estimated input tokens")
        if not self.stdio_command and not self.endpoint.startswith(("unix:///", "ws://", "wss://")):
            raise ValueError("use an explicit Unix socket path, ws://, or wss:// endpoint")
        return self

    @property
    def input_budget(self) -> int:
        limit = min(self.context_limit, self.auto_compact_limit or self.context_limit)
        return limit - self.output_reserve - self.server_overhead - self.safety_margin
