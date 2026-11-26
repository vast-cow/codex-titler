import os
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

MODEL = "gpt-6-luna"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backfill_once: bool = False
    backfill_order: Literal["newest-first", "oldest-first"] = "newest-first"
    endpoint: str = "ws://127.0.0.1:4500"
    unix_uri: str = "ws://localhost/"
    token_env: str | None = None
    provider: str = "openai"
    ephemeral_project_path: str = Field(default="/tmp", min_length=1)
    request_timeout: float = Field(default=30, gt=0)
    generation_timeout: float = Field(default=180, gt=0)
    max_message_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    workers: int = Field(default=2, ge=1, le=32)
    max_title_length: int = Field(default=80, ge=1, le=500)
    safety_tokens: int = Field(default=8192, ge=0)
    output_reserve: int = Field(default=4096, ge=1)
    estimation_policy: Literal["utf8_bytes", "exact_only"] = "utf8_bytes"
    estimate_multiplier: float = Field(default=1.25, ge=1, le=10)
    context_fraction: float = Field(default=0.8, gt=0, le=1)
    generation_attempts: int = Field(default=3, ge=1, le=20)
    transient_failures: int = Field(default=3, ge=1, le=20)
    context_retries: int = Field(default=2, ge=0, le=5)
    reconnect_max: float = Field(default=30, ge=1)
    poll_seconds: float = Field(default=10, gt=0)
    max_pages: int = Field(default=10000, ge=1)

    @field_validator("endpoint")
    @classmethod
    def endpoint_valid(cls, value):
        p = urlsplit(value)
        if p.fragment or p.username or p.password:
            raise ValueError("endpoint must not contain credentials or a fragment")
        if p.scheme == "unix":
            if p.netloc or not p.path.startswith("/") or p.query:
                raise ValueError("use unix:///absolute/path/to/socket")
        elif p.scheme in {"ws", "wss"}:
            if not p.hostname:
                raise ValueError("WebSocket endpoint requires a host")
            _ = p.port
        else:
            raise ValueError("endpoint must use unix:///, ws://, or wss://")
        return value

    @field_validator("unix_uri")
    @classmethod
    def uri_valid(cls, value):
        p = urlsplit(value)
        if p.scheme != "ws" or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError("unix_uri must be a ws:// handshake URI")
        return value

    @property
    def socket_path(self):
        return unquote(urlsplit(self.endpoint).path)

    def headers(self):
        if not self.token_env:
            return None
        value = os.environ.get(self.token_env)
        if not value or "\n" in value or "\r" in value:
            raise ValueError(f"set {self.token_env} to a nonempty bearer token")
        return {"Authorization": f"Bearer {value}"}
