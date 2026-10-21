import asyncio
import unittest
from types import SimpleNamespace as NS
from app.services.streaming_stt import AmazonStreamingTranscription, StreamingTranscriptionError


def result(id='a', text='Ship only after approval', partial=False, start=0, end=.2):
    return NS(result_id=id, alternatives=[NS(transcript=text)], start_time=start, end_time=end, is_partial=partial)


class Provider:
    def __init__(self, final=None, fail_send=False):
        self.audio=[];self.ended=0;self.queue=asyncio.Queue();self.final=final;self.fail_send=fail_send
        self.input_stream=self;self.output_stream=self
    async def send_audio_event(self, *, audio_chunk):
        if self.fail_send:raise RuntimeError('private provider token')
        self.audio.append(audio_chunk)
    async def end_stream(self):
        self.ended+=1
        if self.final:await self.queue.put(self.final)
        await self.queue.put(None)
    def __aiter__(self):return self
    async def __anext__(self):
        event=await self.queue.get()
        if event is None:raise StopAsyncIteration
        return NS(transcript=NS(results=[event]))


class Client:
    def __init__(self,factory=Provider):self.calls=[];self.streams=[];self.factory=factory
    async def start_stream_transcription(self,**kwargs):
        self.calls.append(kwargs);stream=self.factory();self.streams.append(stream);return stream


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_partials_duplicate_finals_and_trailing_silence_barrier(self):
        client=Client();session=await AmazonStreamingTranscription(client=client).start();stream=client.streams[0]
        await session.send(bytes(6400));await session.send(bytes(6400))
        for event in [result(text='Ship',partial=True),result(),result()]:await stream.queue.put(event)
        await session.finish()
        updates=[e async for e in session.updates()]
        self.assertEqual([(e.text,e.final) for e in updates],[('Ship',False),('Ship only after approval',True),('',True)])
        self.assertEqual(updates[-1].end,.4)
        self.assertEqual(len(client.calls),1);self.assertEqual(client.calls[0]['media_encoding'],'pcm')
        await session.finish();await session.abort();self.assertEqual(stream.ended,1)
        with self.assertRaises(StreamingTranscriptionError):await session.send(bytes(2))

    async def test_invalid_frames_never_reach_provider(self):
        client=Client();session=await AmazonStreamingTranscription(client=client).start()
        for pcm in [b'',b'x',bytes(6402)]:
            with self.assertRaises(StreamingTranscriptionError):await session.send(pcm)
        self.assertEqual(client.streams[0].audio,[])
        await session.abort();self.assertTrue(session._reader.done())

    async def test_provider_errors_are_safe_and_reader_is_released(self):
        client=Client(lambda:Provider(fail_send=True));session=await AmazonStreamingTranscription(client=client).start()
        with self.assertRaisesRegex(StreamingTranscriptionError,'MI-STT-001'):await session.send(bytes(6400))
        await session.abort();self.assertTrue(session._reader.done())
        class BadClient:
            async def start_stream_transcription(self,**kwargs):raise RuntimeError('private token and URL')
        with self.assertRaisesRegex(StreamingTranscriptionError,'MI-STT-001'):await AmazonStreamingTranscription(client=BadClient()).start()

    async def test_invalid_and_nonfinite_timestamps_fail_closed(self):
        for start,end in [(0,-1),(float('nan'),1),(0,float('inf'))]:
            client=Client();session=await AmazonStreamingTranscription(client=client).start()
            await client.streams[0].queue.put(result(start=start,end=end))
            with self.assertRaises(StreamingTranscriptionError):await anext(session.updates())
            await session.abort()

    async def test_unexpected_end_is_failure_not_successful_silence(self):
        client=Client();session=await AmazonStreamingTranscription(client=client).start()
        await client.streams[0].queue.put(None)
        with self.assertRaises(StreamingTranscriptionError):await anext(session.updates())
        await session.abort()

    async def test_stalled_noise_rotates_without_overlapping_or_losing_audio(self):
        client=Client();session=await AmazonStreamingTranscription(client=client).start()
        session.max_unfinalized_seconds=.4;frames=[bytes([i])*6400 for i in range(3)]
        for frame in frames:await session.send(frame)
        await session.finish();updates=[e async for e in session.updates()]
        self.assertEqual(len(client.streams),2)
        self.assertEqual(client.streams[0].audio,frames[:2]);self.assertEqual(client.streams[1].audio,frames[2:])
        self.assertEqual([e.end for e in updates],[.4,.6])
        self.assertEqual([s.ended for s in client.streams],[1,1]);await session.abort()

    async def test_regular_finals_preserve_one_context(self):
        client=Client();session=await AmazonStreamingTranscription(client=client).start();session.max_unfinalized_seconds=.4
        for i in range(6):
            await session.send(bytes(6400))
            await client.streams[0].queue.put(result(id=str(i),start=i*.2,end=(i+1)*.2))
            self.assertTrue((await anext(session.updates())).final)
        await session.finish();self.assertEqual(len(client.calls),1);await session.abort()

    async def test_native_callback_future_survives_cancellation_and_connection_closes(self):
        from concurrent.futures import Future
        from amazon_transcribe.httpsession import AwsCrtHttpResponse
        raw=AwsCrtHttpResponse();closed=[]
        def close():
            closed.append(True);future=Future();future.set_result(None);return future
        raw._stream=NS(completion_future=Future(),connection=NS(close=close))
        def factory():
            provider=Provider();provider._raw_stream=raw;return provider
        session=await AmazonStreamingTranscription(client=Client(factory)).start()
        waiting=raw.get_chunk();waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):await waiting
        self.assertFalse(raw._chunk_futures[0].cancelled())
        raw._on_body(b'late provider callback')
        await asyncio.sleep(0);await session.abort();self.assertEqual(closed,[True])

    async def test_failed_drain_cannot_commit_trailing_audio(self):
        client=Client();session=await AmazonStreamingTranscription(client=client).start()
        await session.send(bytes(6400));await client.streams[0].queue.put(result(end=float('nan')))
        with self.assertRaises(StreamingTranscriptionError):await anext(session.updates())
        with self.assertRaises(StreamingTranscriptionError):await session.finish()
        self.assertTrue(session._events.empty());await session.abort()

    async def test_cancelled_start_closes_late_connection_without_cancelling_native_handshake(self):
        from concurrent.futures import Future
        gate=asyncio.Event();closed=[]
        def close():
            closed.append(True);future=Future();future.set_result(None);return future
        provider=Provider();provider._raw_stream=NS(_stream=NS(connection=NS(close=close)))
        class SlowClient:
            async def start_stream_transcription(self,**kwargs):
                await gate.wait();return provider
        session=AmazonStreamingTranscription(client=SlowClient())
        task=asyncio.create_task(session.start());await asyncio.sleep(0);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        gate.set();await asyncio.sleep(0);await asyncio.sleep(0)
        self.assertEqual(closed,[True])


if __name__=='__main__':unittest.main()
