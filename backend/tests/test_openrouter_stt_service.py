import base64
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np

from app.services.openrouter_stt_service import OpenRouterTranscriptionService
from whisperx.audio import SAMPLE_RATE


class OpenRouterTranscriptionServiceTests(unittest.TestCase):
    def test_transcribes_webm_and_returns_whisperx_compatible_shape(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "openai/gpt-transcribe")
            self.assertEqual(payload["response_format"], "json")
            self.assertNotIn("timestamp_granularities", payload)
            self.assertEqual(payload["input_audio"]["format"], "wav")
            wav_bytes = base64.b64decode(payload["input_audio"]["data"])
            with wave.open(io.BytesIO(wav_bytes), "rb") as audio_file:
                self.assertEqual(audio_file.getframerate(), SAMPLE_RATE)
                self.assertEqual(audio_file.getnchannels(), 1)
            return httpx.Response(
                200,
                json={
                    "text": "hello there",
                    "duration": 1.0,
                    "segments": [
                        {"start": 0.1, "end": 0.8, "text": " hello there "}
                    ],
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "window.webm"
            path.write_bytes(b"webm-audio")
            service = OpenRouterTranscriptionService(
                api_key="test-key",
                transport=httpx.MockTransport(respond),
            )
            with patch(
                "app.services.openrouter_stt_service.whisperx.load_audio",
                return_value=np.zeros(SAMPLE_RATE, dtype=np.float32),
            ):
                result = service.transcribe(str(path))

        self.assertEqual(
            result,
            {
                "segments": [{"start": 0.1, "end": 0.8, "text": "hello there"}],
                "audio_duration": 1.0,
                "audio_rms": 0.0, "audio_peak": 0.0,
            },
        )

    def test_falls_back_to_a_full_window_segment_when_timestamps_are_missing(self) -> None:
        def respond(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"text": "spoken words"})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "window.webm"
            path.write_bytes(b"webm-audio")
            service = OpenRouterTranscriptionService(
                api_key="test-key",
                transport=httpx.MockTransport(respond),
            )
            with patch(
                "app.services.openrouter_stt_service.whisperx.load_audio",
                return_value=np.zeros(SAMPLE_RATE * 2, dtype=np.float32),
            ):
                result = service.transcribe(str(path))

        self.assertEqual(
            result,
            {
                "segments": [{"start": 0.0, "end": 2.0, "text": "spoken words"}],
                "audio_duration": 2.0,
                "audio_rms": 0.0, "audio_peak": 0.0,
            },
        )


if __name__ == "__main__":
    unittest.main()