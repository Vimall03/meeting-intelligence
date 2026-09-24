import warnings

warnings.filterwarnings(
    "ignore",
    message=r"\s*torchcodec is not installed correctly.*",
    category=UserWarning,
)

from backend.app.core.config import settings
import whisperx
from whisperx.diarize import DiarizationPipeline


model = whisperx.load_model(
    settings.whisperx_model_size,
    settings.whisperx_device,
    compute_type=settings.whisperx_model_compute_type,
    language="en",
)

def simplify_transcript(result):
    return {
        "segments": [
            {
                "speaker": segment.get("speaker", "UNKNOWN"),
                "start": segment["start"],
                "end": segment["end"],
                "text": segment["text"].strip()
            }
            for segment in result["segments"]
        ]
    }

def transcribe_audio(file_path: str):
    audio = whisperx.load_audio(file_path)
    result = model.transcribe(audio)
    model_a, metadata = whisperx.load_align_model(
        language_code=result["language"],
        device=settings.whisperx_device
    )
    result = whisperx.align(
        result["segments"],
        model_a,
        metadata,
        audio,
        settings.whisperx_device
    )
    
    diarize_model = DiarizationPipeline(device=settings.whisperx_device, token=settings.hf_auth_token)
    diarize_segments = diarize_model(audio)
    
    result = whisperx.assign_word_speakers(
        diarize_segments,
        result
    )
    result = simplify_transcript(result)
    return(result)