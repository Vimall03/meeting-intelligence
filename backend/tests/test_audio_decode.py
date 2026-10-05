import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave

import httpx
import numpy as np

from app.services.audio_decode import load_audio, SAMPLE_RATE
from app.services.openrouter_stt_service import OpenRouterTranscriptionService


class AudioDecodeTests(unittest.TestCase):
    def test_real_webm_decodes_to_mono_16khz_and_reaches_transcription_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / 'source.wav'
            webm = Path(directory) / 'window.webm'
            signal = (.1 * np.sin(2 * np.pi * 440 * np.arange(48000) / 48000) * 32767).astype('<i2')
            with wave.open(str(wav), 'wb') as audio:
                audio.setnchannels(2); audio.setsampwidth(2); audio.setframerate(48000)
                audio.writeframes(np.column_stack((signal, signal)).tobytes())
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(wav), '-c:a', 'libopus', str(webm)], check=True)
            decoded = load_audio(str(webm))
            self.assertEqual(decoded.shape, (SAMPLE_RATE,))
            self.assertGreater(float(np.sqrt(np.mean(decoded ** 2))), .06)
            def respond(request):
                data = json.loads(request.content)
                self.assertEqual(data['input_audio']['format'], 'wav')
                return httpx.Response(200, json={'text': 'hello meeting'})
            service = OpenRouterTranscriptionService('test', transport=httpx.MockTransport(respond))
            result = service.transcribe(str(webm))
            self.assertEqual(result['audio_duration'], 1)
            self.assertEqual(result['segments'][0]['text'], 'hello meeting')

    def test_invalid_audio_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.webm'; path.write_bytes(b'invalid audio')
            with self.assertRaises(RuntimeError): load_audio(str(path))

    def test_startup_imports_no_ml_framework_or_legacy_transcription_route(self):
        script = '''
import importlib.abc, sys
class NoML(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'whisperx', 'torch', 'transformers', 'faster_whisper'}:
            raise AssertionError('Unexpected model dependency: ' + fullname)
sys.meta_path.insert(0, NoML())
from app.main import app
assert not any(getattr(route, 'path', None) == '/transcribe' for route in app.routes)
assert not any(name in sys.modules for name in ['whisperx', 'torch', 'transformers'])
'''
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, check=True, timeout=15)
