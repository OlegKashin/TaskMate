from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    public_base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./taskmate.db"
    redis_url: str = "redis://localhost:6379/0"
    internal_api_token: str = "change-me"
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = "change-me"
    llm_provider: str = "local"
    llm_api_key: str = ""
    llm_model: str = "local-rules"
    stt_provider: str = "disabled"
    stt_api_key: str = ""
    stt_model: str = "gpt-transcribe"
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "taskmate"
    s3_access_key: str = "taskmate"
    s3_secret_key: str = "taskmate-local-secret"
    s3_region: str = "us-east-1"
    google_client_id: str = ""
    google_client_secret: str = ""
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    yandex_client_id: str = ""
    yandex_client_secret: str = ""
    mailru_client_id: str = ""
    mailru_client_secret: str = ""
    imap_host: str = ""
    smtp_host: str = ""
    oauth_state_ttl_seconds: int = 600
    encryption_key: str = ""
    ffmpeg_path: str = "/usr/bin/ffmpeg"
    max_voice_size_bytes: int = 20 * 1024 * 1024
    max_voice_duration_seconds: int = 600
    ai_timeout_seconds: int = 30
    stt_timeout_seconds: int = 30
    scheduler_tick_interval_seconds: int = 60
    ui_actions_cleanup_retention_hours: int = 24
    celery_task_always_eager: bool = False
    undo_ttl_seconds: int = Field(default=15, ge=1)

    @model_validator(mode="after")
    def validate_production_secrets(self):
        if self.app_env.lower() in {"production", "prod"}:
            insecure = {
                "INTERNAL_API_TOKEN": self.internal_api_token in {"", "change-me"},
                "TELEGRAM_WEBHOOK_SECRET": self.telegram_webhook_secret in {"", "change-me"},
                "ENCRYPTION_KEY": not self.encryption_key,
                "TELEGRAM_BOT_TOKEN": not self.telegram_bot_token,
            }
            missing = [name for name, bad in insecure.items() if bad]
            if missing:
                raise ValueError(f"Production secrets must be configured: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
