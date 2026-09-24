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
    

settings = Settings()