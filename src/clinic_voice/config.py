from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./clinic_voice.db"
    public_backend_url: str = "http://localhost:8000"
    webhook_secret: str = "development-only"
    slot_signing_secret: str = "development-slot-secret-change-me"

    pms_provider: str = "mock"
    cliniko_api_key: str = ""
    cliniko_api_base_url: str = "https://api.au1.cliniko.com/v1"
    cliniko_user_agent: str = "clinic-voice-assignment/0.1 admin@example.com"
    cliniko_default_specialty: str = "General Medicine"

    retell_api_key: str = ""
    retell_agent_id: str = ""
    retell_phone_number: str = ""

    clinic_name: str = "VoiceCare Demo Clinic"
    clinic_timezone: str = "Asia/Kolkata"
    clinic_currency: str = "INR"
    same_day_lead_minutes: int = 60
    search_horizon_days: int = 365
    session_resume_minutes: int = 60
    offer_ttl_seconds: int = 120
    cancellation_window_hours: int = 24
    cancellation_fee_minor: int = 30000

    test_phone_returning: str = "+910000000001"
    test_phone_family: str = "+910000000002"
    test_phone_callback: str = "+910000000003"
    test_phone_dropped: str = "+910000000004"
    test_phone_caps_name: str = "+910000000005"

    request_timeout_seconds: float = Field(default=8.0, ge=1, le=30)

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Select the installed Psycopg 3 driver for provider-supplied PostgreSQL URLs."""
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        return value

    @field_validator("pms_provider")
    @classmethod
    def validate_pms_provider(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {"mock", "cliniko"}:
            raise ValueError("PMS_PROVIDER must be mock or cliniko")
        return normalized

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.clinic_timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
