"""One paid recognition stream per meeting, with bounded final-result stalls.

Only finalized text and explicit drained-audio barriers can be acknowledged.
Audio stays in the browser replay buffer; no per-frame storage or model calls.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
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
    max_frame_bytes = 6400  # 200 ms mono signed PCM16
    # Flush stalled speech/noise/silence without cutting every short audio frame.
    # Normal finalized speech keeps one context for up to three hours.
    max_unfinalized_seconds = 30
    max_context_seconds = 10800

    def __init__(self, *, region='ap-south-1', language='en-US', profile=None, client=None):
        self.region, self.language, self.profile = region, language, profile
        self.client = client
        self.stream = None
        self.closed = False
        self.audio_bytes = 0
        self._offset = self._final_end = 0.0
        self._reader = None
        self._ending_stream = None
        self._failed = False
        self._discard = False
        self._events = asyncio.Queue(maxsize=64)

    async def start(self):
        try:
            if self.client is None:
                session = boto3.Session(profile_name=self.profile, region_name=self.region)
                self.client = TranscribeStreamingClient(region=self.region,
                    credential_resolver=BotoCredentials(session))
            await self._open()
        except Exception as exc:
            raise StreamingTranscriptionError() from exc
        return self

    async def _open(self):
        opening = asyncio.create_task(self.client.start_stream_transcription(
            language_code=self.language, media_sample_rate_hz=self.sample_rate,
            media_encoding='pcm', enable_partial_results_stabilization=True,
            partial_results_stability='medium'))
        try:
            self.stream = await asyncio.wait_for(asyncio.shield(opening), timeout=15)
        except BaseException:
            # A cancelled HTTP handshake must not cancel the SDK's native futures.
            # If it completes late, immediately close the unclaimed connection.
            def release_late_stream(task):
                with contextlib.suppress(BaseException):
                    raw = task.result().output_stream._raw_stream
                    raw._stream.connection.close()
            opening.add_done_callback(release_late_stream)
            raise
        # SDK 0.6.4 wraps native callback futures without shielding cancellation.
        # Scope the fix to this response: cancelling our reader must not cancel a
        # future that the native HTTP callback will subsequently complete.
        raw = getattr(self.stream.output_stream, '_raw_stream', None)
        if raw is not None:
            get_chunk = raw.get_chunk
            raw.get_chunk = lambda: asyncio.shield(get_chunk())
        self._reader = asyncio.create_task(self._read(self.stream, self._offset))

    async def _read(self, stream, offset):
        final_ids = set()
        try:
            async for event in stream.output_stream:
                for result in event.transcript.results:
                    if not result.alternatives or result.result_id in final_ids:
                        continue
                    text = result.alternatives[0].transcript.strip()
                    if not text:
                        continue
                    start, end = float(result.start_time), float(result.end_time)
                    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                        raise StreamingTranscriptionError()
                    final = not result.is_partial
                    if final:
                        final_ids.add(result.result_id)
                        self._final_end = max(self._final_end, offset + end)
                    if not self._discard:
                        await self._events.put(TranscriptUpdate(result.result_id, text,
                            offset + start, offset + end, final))
            if self._ending_stream is not stream:
                raise StreamingTranscriptionError()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failed = True
            if not self._discard:
                await self._events.put(StreamingTranscriptionError())

    async def _drain(self):
        self._ending_stream = self.stream
        await asyncio.wait_for(self.stream.input_stream.end_stream(), timeout=5)
        await asyncio.wait_for(asyncio.shield(self._reader), timeout=15)
        if self._failed:
            raise StreamingTranscriptionError()
        # Empty final barrier commits the trailing silence only after all results.
        await self._events.put(TranscriptUpdate('', '', self.audio_seconds, self.audio_seconds, True))
        self._final_end = self.audio_seconds

    async def send(self, pcm: bytes):
        if self.closed or self._failed or self.stream is None or not pcm or len(pcm) % 2 or len(pcm) > self.max_frame_bytes:
            raise StreamingTranscriptionError()
        try:
            if (self.audio_seconds - self._final_end >= self.max_unfinalized_seconds
                    or self.audio_seconds - self._offset >= self.max_context_seconds):
                await self._drain()
                self._offset = self.audio_seconds
                await self._open()
            await asyncio.wait_for(self.stream.input_stream.send_audio_event(audio_chunk=pcm), timeout=5)
            self.audio_bytes += len(pcm)
        except Exception as exc:
            raise StreamingTranscriptionError() from exc

    async def finish(self):
        if self.stream is None or self.closed:
            return
        self.closed = True
        try:
            await self._drain()
            await self._events.put(None)
        except Exception as exc:
            raise StreamingTranscriptionError() from exc

    async def abort(self):
        """End paid input, drain native callbacks, then release the connection."""
        self._discard = True
        if not self.closed and self.stream is not None:
            self.closed = True
            self._ending_stream = self.stream
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.stream.input_stream.end_stream(), timeout=2)
        if self._reader:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._reader), timeout=5)
        # The SDK has no public client close; each instance owns its connection.
        # Close that transport even on a timeout rather than leave it running.
        raw = getattr(getattr(self.stream, 'output_stream', None), '_raw_stream', None)
        native = getattr(raw, '_stream', None)
        if native is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(native.connection.close())), timeout=2)
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader

    async def updates(self):
        while True:
            event = await self._events.get()
            if event is None:
                return
            if isinstance(event, Exception):
                raise event
            yield event

    @property
    def audio_seconds(self):
        return self.audio_bytes / (self.sample_rate * 2)
