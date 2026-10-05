from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
    )

    hf_auth_token: str = Field(default="", validation_alias="HUGGING_FACE_AUTH_TOKEN")
    aws_region: str = "us-east-1"
    auth_user_pool_id: str = ""
    auth_client_id: str = ""
    auth_api_url: str = ""
    auth_table_name: str = ""
    integration_key_id: str = ""
    extension_origin: str = ""
    
    whisperx_model_size: str = "small"
    whisperx_device: str = "cpu"
    whisperx_model_compute_type: str = "int8"
    meeting_context_window_seconds: float = 45.0
    meeting_context_turn_gap_seconds: float = 12.0
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    openrouter_stt_model: str = Field(
        default="openai/gpt-transcribe", validation_alias="OPENROUTER_STT_MODEL"
    )
    openrouter_stt_url: str = Field(
        default="https://openrouter.ai/api/v1/audio/transcriptions",
        validation_alias="OPENROUTER_STT_URL",
    )
    jev_model: str = "typesafe/jev-1.13"
    jev_decisions_url: str = "https://openrouter.ai/api/alpha/decisions"
    jev_search_threshold: float = 0.75
    jev_task_threshold: float = 0.75
    jev_dedup_window_seconds: float = 90.0
    gemma_model: str = "google/gemma-4-26b-a4b-it"
    gemma_chat_completions_url: str = "https://openrouter.ai/api/v1/chat/completions"

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
