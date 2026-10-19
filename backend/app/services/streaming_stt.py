"""Persistent speech-to-text session; credentials and audio stay server-side.

Only final results may enter meeting context. Partial results are replaceable UI
previews. A session is owned by one authenticated meeting, never a shared cache.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import boto3
from amazon_transcribe.auth import CredentialResolver, Credentials
from amazon_transcribe.client import TranscribeStreamingClient


class StreamingTranscriptionError(RuntimeError):
    def __init__(self):
        super().__init__('MI-STT-001')


@dataclass(frozen=True)
class TranscriptUpdate:
    result_id: str
    text: str
    start: float
    end: float
    final: bool


class BotoCredentials(CredentialResolver):
    def __init__(self, session):
        self.session = session

    async def get_credentials(self):
        def resolve():
            value = self.session.get_credentials()
            if value is None:
                raise StreamingTranscriptionError()
            value = value.get_frozen_credentials()
            return Credentials(value.access_key, value.secret_key, value.token)
        return await asyncio.to_thread(resolve)


class AmazonStreamingTranscription:
    sample_rate = 16000
    # 200 ms mono signed 16-bit PCM; oversized input is rejected before sending.
    max_frame_bytes = 6400

    def __init__(self, *, region='ap-south-1', language='en-US', profile=None, client=None):
        self.region, self.language, self.profile = region, language, profile
        self.client = client
        self.stream = None
        self.closed = False
        self.audio_bytes = 0
        self._final_ids = set()

    async def start(self):
        try:
            if self.client is None:
                session = boto3.Session(profile_name=self.profile, region_name=self.region)
                self.client = TranscribeStreamingClient(region=self.region,
                    credential_resolver=BotoCredentials(session))
            self.stream = await asyncio.wait_for(self.client.start_stream_transcription(
                language_code=self.language, media_sample_rate_hz=self.sample_rate,
                media_encoding='pcm', enable_partial_results_stabilization=True,
                partial_results_stability='medium'), timeout=15)
        except Exception as exc:
            raise StreamingTranscriptionError() from exc
        return self

    async def send(self, pcm: bytes):
        if self.closed or self.stream is None or not pcm or len(pcm) % 2 or len(pcm) > self.max_frame_bytes:
            raise StreamingTranscriptionError()
        try:
            await asyncio.wait_for(self.stream.input_stream.send_audio_event(audio_chunk=pcm), timeout=5)
            self.audio_bytes += len(pcm)
        except Exception as exc:
            raise StreamingTranscriptionError() from exc

    async def finish(self):
        if self.stream is not None and not self.closed:
            self.closed = True
            try:
                await asyncio.wait_for(self.stream.input_stream.end_stream(), timeout=5)
            except Exception as exc:
                raise StreamingTranscriptionError() from exc

    async def updates(self):
        if self.stream is None:
            raise StreamingTranscriptionError()
        try:
            async for event in self.stream.output_stream:
                for result in event.transcript.results:
                    if not result.alternatives:
                        continue
                    text = result.alternatives[0].transcript.strip()
                    final = not result.is_partial
                    if not text or result.result_id in self._final_ids:
                        continue
                    start, end = float(result.start_time), float(result.end_time)
                    if start < 0 or end < start:
                        raise StreamingTranscriptionError()
                    if final:
                        self._final_ids.add(result.result_id)
                    yield TranscriptUpdate(result.result_id, text, start, end, final)
        except Exception as exc:
            raise StreamingTranscriptionError() from exc

    @property
    def audio_seconds(self):
        return self.audio_bytes / (self.sample_rate * 2)
