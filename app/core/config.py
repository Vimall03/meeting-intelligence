from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    whisper_model_size:str = "small"
    whisper_model_device:str = "cpu" # im using cpu on mac, use cuda if your device has a nvidia GPU
    whisper_model_compute_type: str = "int8" # use float8 for better accuracy 
    hf_auth_token: str = ""

settings = Settings()