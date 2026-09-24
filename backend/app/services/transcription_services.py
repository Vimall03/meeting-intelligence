import warnings

warnings.filterwarnings(
    "ignore",
    message=r"\s*torchcodec is not installed correctly.*",
    category=UserWarning,
)

from app.core.config import settings
import whisperx
from whisperx.audio import SAMPLE_RATE
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

def transcribe_realtime_chunk(file_path: str, start_seconds: float = 0.0):
    """
    Fast transcription for live meeting audio chunks.
    No alignment.
    No diarization.
    """

    audio = whisperx.load_audio(file_path)
    audio_duration = len(audio) / SAMPLE_RATE
    chunk_start = min(start_seconds, audio_duration)
    audio = audio[int(chunk_start * SAMPLE_RATE):]

    if not len(audio):
        return {"segments": [], "audio_duration": audio_duration}

    result = model.transcribe(audio)

    return {
        "segments": [
            {
                "start": segment["start"] + chunk_start,
                "end": segment["end"] + chunk_start,
                "text": segment["text"].strip()
            }
            for segment in result["segments"]
            if segment["text"].strip()
        ],
        "audio_duration": audio_duration,
    }