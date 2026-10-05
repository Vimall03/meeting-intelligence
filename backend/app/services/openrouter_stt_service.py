import base64
import io
import wave

import httpx
import numpy as np
from app.services.audio_decode import SAMPLE_RATE, load_audio

from app.core.config import settings


class OpenRouterTranscriptionService:
    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-transcribe",
        endpoint: str = "https://openrouter.ai/api/v1/audio/transcriptions",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.transport = transport

    def transcribe(self, file_path: str, start_seconds: float = 0.0) -> dict:
        audio = load_audio(file_path)
        audio_duration = len(audio) / SAMPLE_RATE
        chunk_start = min(max(start_seconds, 0.0), audio_duration)
        if not len(audio) or chunk_start >= audio_duration:
            return {"segments": [], "audio_duration": audio_duration}

        audio_data = self._encode_wav(audio[int(chunk_start * SAMPLE_RATE) :])

        payload = {
            "model": self.model,
            "input_audio": {
                "data": base64.b64encode(audio_data).decode("ascii"),
                "format": "wav",
            },
            "response_format": "json",
        }
        with httpx.Client(
            timeout=60.0,
            transport=self.transport,
        ) as client:
            response = client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            if not response.is_success:
                raise httpx.HTTPStatusError(
                    f"OpenRouter transcription failed ({response.status_code}): "
                    f"{response.text}",
                    request=response.request,
                    response=response,
                )

        result = response.json()
        segments = [
            {
                "start": float(segment.get("start", 0.0)) + chunk_start,
                "end": float(segment.get("end", audio_duration - chunk_start))
                + chunk_start,
                "text": segment["text"].strip(),
            }
            for segment in result.get("segments", [])
            if segment.get("text", "").strip()
        ]
        if not segments and result.get("text", "").strip():
            segments.append(
                {
                    "start": chunk_start,
                    "end": audio_duration,
                    "text": result["text"].strip(),
                }
            )

        return {
            "segments": segments, "audio_duration": audio_duration,
            "audio_rms": float(np.sqrt(np.mean(np.square(audio)))),
            "audio_peak": float(np.max(np.abs(audio))),
        }

    @staticmethod
    def _encode_wav(audio: np.ndarray) -> bytes:
        pcm_audio = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(pcm_audio.tobytes())
        return output.getvalue()


def transcribe_realtime_chunk(file_path: str, start_seconds: float = 0.0) -> dict:
    service = OpenRouterTranscriptionService(
        api_key=settings.openrouter_api_key,
        model=settings.openrouter_stt_model,
        endpoint=settings.openrouter_stt_url,
    )
    return service.transcribe(file_path, start_seconds)
