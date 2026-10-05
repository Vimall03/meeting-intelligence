"""Decode bounded meeting windows with FFmpeg, without loading ML models."""
import subprocess

import numpy as np

SAMPLE_RATE = 16000


def load_audio(file_path: str) -> np.ndarray:
    try:
        result = subprocess.run(
            ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
             '-protocol_whitelist', 'file,pipe', '-i', file_path,
             '-t', '13', '-f', 'f32le', '-acodec', 'pcm_f32le', '-ac', '1',
             '-ar', str(SAMPLE_RATE), 'pipe:1'],
            capture_output=True, check=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Meeting audio could not be decoded') from exc
    audio = np.frombuffer(result.stdout, dtype=np.float32).copy()
    if len(audio) > 12 * SAMPLE_RATE:
        raise RuntimeError('Meeting audio window exceeds the supported duration')
    return audio
