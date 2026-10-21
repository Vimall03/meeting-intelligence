from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        # Older local .env files may still contain retired model settings.
        extra="ignore",
    )

    aws_region: str = "us-east-1"
    auth_user_pool_id: str = ""
    auth_client_id: str = ""
    auth_api_url: str = ""
    auth_table_name: str = ""
    integration_key_id: str = ""
    extension_origin: str = ""
    
    meeting_context_window_seconds: float = 45.0
    streaming_stt_language: str = 'en-US'
    streaming_max_connections: int = Field(default=4, ge=1, le=16)
    meeting_background_window_seconds: float = Field(default=180.0, gt=0)
    meeting_context_turn_gap_seconds: float = 12.0
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    jev_model: str = "typesafe/jev-1.13"
    jev_decisions_url: str = "https://openrouter.ai/api/alpha/decisions"
    jev_search_threshold: float = 0.75
    jev_task_threshold: float = 0.75
    intent_review_minimum: float = Field(default=0.3, ge=0.0, le=1.0)
    jev_dedup_window_seconds: float = 90.0
    gemma_model: str = "google/gemma-4-26b-a4b-it"
    gemma_chat_completions_url: str = "https://openrouter.ai/api/v1/chat/completions"

    # Backend-only MOM preparation. Global availability and per-user opt-in are separate.
    mom_enabled: bool = False
    mom_table_name: str = ""
    mom_bucket_name: str = ""
    mom_key_arn: str = ""
    mom_queue_url: str = ""
    mom_grace_seconds: int = Field(default=600, ge=60, le=3600)
    mom_retention_seconds: int = Field(default=86400, ge=3600, le=86400)
    mom_max_session_seconds: int = Field(default=21600, ge=60, le=21600)

    # Google Drive OAuth and API settings. The refresh token is persisted by
    # the backend after the one-time browser authorization flow.
    google_drive_client_id: str = Field(default="", validation_alias="GOOGLE_DRIVE_CLIENT_ID")
    google_drive_client_secret: str = Field(default="", validation_alias="GOOGLE_DRIVE_CLIENT_SECRET")
    google_drive_redirect_uri: str = Field(
        default="http://localhost:8000/api/integrations/google-drive/oauth/callback",
        validation_alias="GOOGLE_DRIVE_REDIRECT_URI",
    )
    google_drive_oauth_state_secret: str = Field(
        default="", validation_alias="GOOGLE_DRIVE_OAUTH_STATE_SECRET"
    )
    google_drive_token_path: str = Field(
        default=".data/google_drive_token.json",
        validation_alias="GOOGLE_DRIVE_TOKEN_PATH",
    )
    google_drive_scopes: str = Field(
        default="https://www.googleapis.com/auth/drive.readonly",
        validation_alias="GOOGLE_DRIVE_SCOPES",
    )
    google_drive_timeout_seconds: float = Field(
        default=8.0, validation_alias="GOOGLE_DRIVE_TIMEOUT_SECONDS"
    )

    atlassian_client_id: str = Field(default="", validation_alias="ATLASSIAN_CLIENT_ID")
    atlassian_client_secret: str = Field(default="", validation_alias="ATLASSIAN_CLIENT_SECRET")
    atlassian_site_url: str = Field(default="", validation_alias="ATLASSIAN_SITE_URL")
    atlassian_redirect_uri: str = Field(
        default="http://localhost:8000/api/integrations/atlassian/oauth/callback",
        validation_alias="ATLASSIAN_REDIRECT_URI",
    )
    atlassian_oauth_state_secret: str = Field(default="", validation_alias="ATLASSIAN_OAUTH_STATE_SECRET")
    atlassian_token_path: str = Field(default=".data/atlassian_token.json", validation_alias="ATLASSIAN_TOKEN_PATH")
    atlassian_timeout_seconds: float = Field(default=8.0, validation_alias="ATLASSIAN_TIMEOUT_SECONDS")
    

settings = Settings()
