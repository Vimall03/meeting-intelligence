"""Provider fixture: real PCM transport, deterministic finalized speech, no API calls."""
import asyncio
from app.services.streaming_stt import TranscriptUpdate


def transcription_stream(recognize):
    class Stream:
        def __init__(self, **kwargs):
            self.audio_seconds = 0.0
            self.closed = False
            self.events = asyncio.Queue()
            self.frames = 0

        async def start(self):
            return self

        async def send(self, pcm):
            result = await recognize(pcm)
            offset = self.audio_seconds
            duration = len(pcm) / 32000
            self.audio_seconds += duration
            self.frames += 1
            scale = duration / result['audio_duration'] if result['audio_duration'] else 0
            for segment in result['segments']:
                await self.events.put(TranscriptUpdate(str(self.frames), segment['text'],
                    offset + segment['start'] * scale, offset + segment['end'] * scale, True))

        async def finish(self):
            if self.closed:
                return
            self.closed = True
            await self.events.put(TranscriptUpdate('', '', self.audio_seconds, self.audio_seconds, True))
            await self.events.put(None)

        async def abort(self):
            self.closed = True

        async def updates(self):
            while (event := await self.events.get()) is not None:
                yield event
    return Stream


async def default_speech(pcm):
    return {'audio_duration': .2, 'segments': [{'start': 0, 'end': .2,
        'text': 'Please send the launch plan to Maya.'}]}


DefaultStream = transcription_stream(default_speech)
