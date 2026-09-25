from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
    )

    hf_auth_token: str = Field(default="", validation_alias="HUGGING_FACE_AUTH_TOKEN")
    
    whisperx_model_size: str = "small"
    whisperx_device: str = "cpu"
    whisperx_model_compute_type: str = "int8"
    meeting_context_window_seconds: float = 45.0
    meeting_context_turn_gap_seconds: float = 12.0
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    jev_model: str = "typesafe/jev-1.13"
    jev_decisions_url: str = "https://openrouter.ai/api/alpha/decisions"
    jev_search_threshold: float = 0.75
    jev_task_threshold: float = 0.75
    jev_dedup_window_seconds: float = 90.0
    gemma_model: str = "google/gemma-4-26b-a4b-it"
    gemma_chat_completions_url: str = "https://openrouter.ai/api/v1/chat/completions"
    

settings = Settings()