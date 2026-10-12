"""Configuration loaded without importing third-party settings libraries."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Runtime settings for this initial local deployment."""

    database_path: Path
    environment: str
    call_provider: str
    internal_webhook_token: str | None
    public_base_url: str | None
    exotel_api_key: str | None
    exotel_api_token: str | None
    exotel_account_sid: str | None
    exotel_caller_id: str | None
    exotel_base_url: str


def get_settings() -> Settings:
    configured_path = Path(os.getenv("DATABASE_PATH", "data/campaigner.db"))
    database_path = (
        configured_path
        if configured_path.is_absolute()
        else PROJECT_ROOT / configured_path
    )
    call_provider = os.getenv("CALL_PROVIDER", "mock").lower()
    if call_provider not in {"mock", "exotel"}:
        raise ValueError("CALL_PROVIDER must be either 'mock' or 'exotel'")

    return Settings(
        database_path=database_path,
        environment=os.getenv("APP_ENV", "development"),
        call_provider=call_provider,
        internal_webhook_token=os.getenv("INTERNAL_WEBHOOK_TOKEN") or None,
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/") or None,
        exotel_api_key=os.getenv("EXOTEL_API_KEY") or None,
        exotel_api_token=os.getenv("EXOTEL_API_TOKEN") or None,
        exotel_account_sid=os.getenv("EXOTEL_ACCOUNT_SID") or None,
        exotel_caller_id=os.getenv("EXOTEL_CALLER_ID") or None,
        exotel_base_url=os.getenv("EXOTEL_BASE_URL", "https://api.exotel.com").rstrip("/"),
    )

