from faster_whisper import WhisperModel
from app.core.config import settings

model_size = settings.whisper_model_size

print(f"Loading Whisper model... {model_size}")

model = WhisperModel(model_size, device=settings.whisper_model_device, compute_type=settings.whisper_model_compute_type) 

print("Whisper model loaded successfully")

def transcribe_audio(file_path: str):
    segments, info = model.transcribe(file_path)
    
    text = " ".join(segment.text for segment in segments) 
    return {
        "text": text,
        "language": info.language
    }
    