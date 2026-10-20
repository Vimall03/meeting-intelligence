import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from stt_aws_context_control import event, paired_replay, reservation, summarise, windows
from stt_aws_context_report import render


class ContextControlTests(unittest.TestCase):
    def test_context_partition_preserves_every_sample_once(self):
        pcm = bytes(range(256)) * 401
        parts = windows(pcm)
        self.assertEqual([offset for offset, _ in parts], [0, 3])
        self.assertEqual(b''.join(audio for _, audio in parts), pcm)
        self.assertTrue(all(len(audio) % 2 == 0 for _, audio in parts))

    def test_budget_reserves_historical_minimum_for_each_session(self):
        self.assertAlmostEqual(reservation(3.2), .0045)
        self.assertAlmostEqual(reservation(15), .009)

    def test_session_result_ids_are_unique_and_order_uses_source_offset(self):
        update = SimpleNamespace(result_id='same', start=.1, text='hello', final=True)
        a = event(update, context=0, offset=0, received=2)
        b = event(update, context=1, offset=3, received=4)
        self.assertNotEqual(a['id'], b['id'])
        self.assertAlmostEqual(b['order'], 3.1)

    def test_empty_experiment_does_not_claim_zero_wer_or_latency(self):
        result = summarise([])
        self.assertIsNone(result['continuous']['wer'])
        self.assertIsNone(result['reset']['final_latency_p50_s'])

    def test_report_keeps_peer_streaming_unmeasured_and_pacing_limits_visible(self):
        report = render({'rows': [], 'reservations': []})
        self.assertIn('different providers using genuine continuous audio remains incomplete', report)
        self.assertIn('not guaranteed identical arrival timing', report)
        self.assertIn('0 successful, 0 failed', report)
        self.assertIn('| Uninterrupted recognition context | — | — / — |', report)


class StreamingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    class Session:
        instances = []

        def __init__(self, **kwargs):
            self.frames = []; self.closed = False; self.queue = asyncio.Queue()
            self.instances.append(self)

        async def start(self):
            return self

        async def send(self, frame):
            self.frames.append(frame)

        async def finish(self):
            if not self.closed:
                self.closed = True
                self.queue.put_nowait(SimpleNamespace(result_id='same', start=0,
                    text='hello', final=True))
                self.queue.put_nowait(None)

        async def updates(self):
            while (item := await self.queue.get()) is not None:
                yield item

    async def test_live_pacing_preserves_source_and_closes_all_contexts(self):
        self.Session.instances = []
        pcm = bytes(range(256)) * 400
        with patch('stt_aws_context_control.AmazonStreamingTranscription', self.Session):
            result = await paired_replay(pcm, region='test', profile='test')
        sessions = self.Session.instances
        self.assertEqual(len(sessions), 3)
        self.assertEqual(b''.join(sessions[0].frames), pcm)
        self.assertEqual(b''.join(frame for session in sessions[1:] for frame in session.frames), pcm)
        self.assertTrue(all(s.closed for s in sessions))
        self.assertTrue(all(len(frame) <= 6400 for s in sessions for frame in s.frames))
        self.assertEqual(len(result['events']['reset']), 2)

    async def test_initial_session_failure_cancels_and_closes_both_arms(self):
        self.Session.instances = []
        class Failed(self.Session):
            async def start(self):
                raise RuntimeError('unit failure')
        with patch('stt_aws_context_control.AmazonStreamingTranscription', Failed):
            with self.assertRaises(RuntimeError):
                await paired_replay(bytes(6400), region='test', profile='test')
        self.assertTrue(all(session.closed for session in self.Session.instances))


if __name__ == '__main__':
    unittest.main()
