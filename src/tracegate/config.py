from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRACEGATE_", env_file=".env", extra="ignore")

    service_name: str = "tracegate"
    project_name: str = "tracegate"
    otlp_endpoint: str | None = None
    otlp_headers: str = ""
    console_export: bool = False
    budget_usd: float = Field(default=2.0, gt=0)
    max_steps: int = Field(default=25, ge=1)
    loop_repeats: int = Field(default=3, ge=2)
    loop_max_period: int = Field(default=3, ge=1)
    max_payload_chars: int = Field(default=8000, ge=64)
    capture_budget_ratio: float = Field(default=0.8, gt=0, le=1)
    capture_step_ratio: float = Field(default=0.8, gt=0, le=1)
    pricing_file: str | None = None

    @field_validator("otlp_endpoint", "pricing_file", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def otlp_header_map(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for pair in self.otlp_headers.split(","):
            pair = pair.strip()
            if not pair:
                continue
            key, separator, value = pair.partition("=")
            if not separator or not key.strip():
                raise ValueError(f"invalid OTLP header entry: {pair!r}")
            headers[key.strip()] = value.strip()
        return headers
