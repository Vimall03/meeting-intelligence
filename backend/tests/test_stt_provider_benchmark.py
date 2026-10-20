import asyncio
import argparse
import contextlib
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

from stt_provider_benchmark import (
    BenchmarkError, EnergyVAD, EventParser, PROVIDERS, measured_cost,
    main, read_audio, safe_failure, score_events, summarise, write_json,
    run_socket,
)
from stt_benchmark_report import cost_for_report, render


def clip(text='Authentication works', end=2):
    return {'utterances': [{'text': text, 'start': .5, 'end': end}]}


class ScoringTests(unittest.TestCase):
    def test_subwords_across_packets_are_joined_and_timed_at_last_piece(self):
        parser = EventParser('soniox')
        events = []
        for index, text in enumerate(['Authen', 'tication', ' works']):
            events += parser.parse({'tokens': [{'text': text, 'is_final': True}]}, 2 + index / 10)
        stats = score_events(clip(), events)
        self.assertEqual(stats['wer'], 0)
        self.assertAlmostEqual(stats['indexed_final_lags'][0]['lag_s'], .2)

    def test_partials_and_protocol_markers_never_enter_accuracy(self):
        parser = EventParser('soniox')
        events = parser.parse({'tokens': [
            {'text': 'wrong preview', 'is_final': False},
            {'text': 'Authentication works', 'is_final': True},
            {'text': '<end>', 'is_final': True}]}, 3)
        stats = score_events(clip(), events)
        self.assertEqual(stats['wer'], 0)
        self.assertEqual(stats['first_partial_after_speech_s'], 2.5)

    def test_duplicate_finals_not_counted_twice(self):
        parser = EventParser('assemblyai')
        data = {'type': 'Turn', 'transcript': 'Authentication works', 'end_of_turn': True, 'turn_order': 0}
        events = parser.parse(data, 3) + parser.parse(data, 5)
        stats = score_events(clip(), events)
        self.assertEqual(stats['wer'], 0)
        self.assertEqual(stats['indexed_final_lags'][0]['lag_s'], 1)

    def test_openai_completed_turns_are_in_audio_order_not_arrival_order(self):
        parser = EventParser('openai_live'); events = []
        for item, previous in [('z_first', None), ('a_second', 'z_first')]:
            parser.parse({'type': 'input_audio_buffer.committed', 'item_id': item, 'previous_item_id': previous}, 0)
        for item, text, received in [('a_second', 'works', 3), ('z_first', 'Authentication', 4)]:
            events += parser.parse({'type': 'conversation.item.input_audio_transcription.completed',
                'item_id': item, 'transcript': text}, received)
        parser.order_openai(events)
        stats = score_events(clip(), events)
        self.assertEqual(stats['wer'], 0)
        self.assertEqual(stats['final_text'], 'Authentication works')

    def test_openai_missing_audio_order_fails_instead_of_inventing_order(self):
        parser = EventParser('openai_live')
        events = parser.parse({'type': 'conversation.item.input_audio_transcription.completed',
            'item_id': 'unknown', 'transcript': 'hello'}, 1)
        with self.assertRaises(BenchmarkError):
            parser.order_openai(events)

    def test_missing_final_word_counts_in_wer_and_latency_coverage(self):
        stats = score_events(clip(), [{'final': True, 'id': 'x', 'order': 0,
            'text': 'Authentication', 'received_s': 2.1}])
        self.assertEqual(stats['wer'], .5)
        self.assertEqual(stats['latency_coverage'], 0)
        self.assertEqual(stats['utterance_count'], 1)

    def test_scorer_agrees_with_existing_baseline_on_all_saved_runs(self):
        from stt_compare import score
        root = Path(__file__).resolve().parents[2] / 'docs/benchmarks/stt-2026-10-19'
        manifest = json.loads((root / 'manifest.json').read_text())
        clips = {c['id']: c for c in manifest['clips']}
        for row in json.loads((root / 'results.json').read_text())['rows']:
            for provider in ('batch', 'streaming'):
                events = [{**event, 'id': str(i), 'order': i}
                    for i, event in enumerate(row[provider]['events'])]
                previous = score(clips[row['clip']], events)
                current = score_events(clips[row['clip']], events)
                self.assertEqual(current['wer'], previous['wer'])
                self.assertEqual(current['indexed_final_lags'], previous['indexed_final_lags'])


class ProtocolAndCostTests(unittest.TestCase):
    def test_deepgram_final_does_not_require_speech_final(self):
        parser = EventParser('deepgram')
        events = parser.parse({'type': 'Results', 'is_final': True, 'speech_final': False,
            'start': 0, 'channel': {'alternatives': [{'transcript': 'hello'}]}}, 2)
        self.assertTrue(events[0]['final'])

    def test_assembly_session_duration_includes_idle_time(self):
        parser = EventParser('assemblyai')
        parser.parse({'type': 'Termination', 'audio_duration_seconds': 45, 'session_duration_seconds': 60}, 60)
        cost, basis = measured_cost('assemblyai', PROVIDERS['assemblyai'], 45, {'provider_usage': parser.usage})
        self.assertAlmostEqual(cost, .15 / 60)
        self.assertIn('reported_session_duration', basis)
        self.assertTrue(parser.done)

    def test_estimate_not_mislabeled_as_actual_charge(self):
        cost, basis = measured_cost('soniox', PROVIDERS['soniox'], 60, {})
        self.assertAlmostEqual(cost, .12 / 60)
        self.assertEqual(basis, 'published_rate_estimate_not_invoice')

    def test_openrouter_reported_costs_are_preserved(self):
        cost, basis = measured_cost('openrouter_chunks', PROVIDERS['openrouter_chunks'], 60,
            {'provider_usage': {'costs_usd': [.001, .002]}})
        self.assertAlmostEqual(cost, .003)
        self.assertEqual(basis, 'provider_reported')

    def test_provider_error_body_and_secrets_are_not_saved(self):
        parser = EventParser('soniox')
        try:
            parser.parse({'error_code': 401, 'error_message': 'secret-example-key'}, 0)
        except BenchmarkError as exc:
            failure = safe_failure(exc)
        self.assertNotIn('secret', json.dumps(failure))
        self.assertEqual(failure['code'], 'MI-BENCH-001')

    def test_unsupported_provider_output_does_not_silently_create_final_text(self):
        self.assertEqual(EventParser('deepgram').parse({'type': 'Unknown'}, 0), [])

    def test_no_result_is_not_zero_error_rate(self):
        summary = summarise([], list(PROVIDERS), 3, 10)
        self.assertIsNone(summary['soniox']['wer'])
        self.assertIsNone(summary['soniox']['final_latency_p50_s'])
        self.assertEqual(summary['soniox']['status'], 'not_run')

    def test_failed_run_reservation_remains_visible(self):
        summary = summarise([{'provider': 'soniox', 'status': 'failed', 'reserved_usd': .01}], ['soniox'], 3, 10)
        self.assertEqual(summary['soniox']['status'], 'failed')
        self.assertEqual(summary['soniox']['unreconciled_failed_cost_reserve_usd'], .01)

    def test_causal_vad_commits_after_silence_without_reference_timing(self):
        vad = EnergyVAD()
        silence = bytes(6400)
        speech = (1000).to_bytes(2, 'little', signed=True) * 3200
        self.assertFalse(vad.consume(silence))
        self.assertFalse(vad.consume(silence))
        self.assertFalse(vad.consume(speech))
        self.assertFalse(vad.consume(silence))
        self.assertTrue(vad.consume(silence))
        self.assertFalse(vad.consume(silence))

    def test_atomic_write_is_valid_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'result.json'
            write_json(path, {'reservations': [1]})
            self.assertEqual(json.loads(path.read_text()), {'reservations': [1]})
            self.assertFalse(path.with_suffix('.tmp').exists())

    def test_audio_paths_cannot_escape_corpus(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                read_audio(Path(folder), {'audio': '../private.wav'})


class SpendBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setup_run(self, directory):
        folder = Path(directory); audio = folder / 'test.wav'
        with wave.open(str(audio), 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(bytes(6400))
        manifest = folder / 'manifest.json'
        manifest.write_text(json.dumps({'clips': [{'id': 'test', 'audio': audio.name,
            'sha256': hashlib.sha256(audio.read_bytes()).hexdigest(), 'duration_s': .2,
            'category': 'unit_fixture', 'utterances': [{'text': 'hello', 'start': 0, 'end': .1}]}]}))
        return argparse.Namespace(manifest=manifest, providers='openrouter_chunks', repeats=3,
            profile=None, region='ap-south-1', max_estimated_usd=1,
            execute=True, output=folder / 'result.json')

    async def test_default_plan_makes_no_provider_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.setup_run(folder); args.execute = False
            with patch('stt_provider_benchmark.credentials', return_value={'OPENROUTER_API_KEY': 'never-log-this'}), \
                    patch('stt_provider_benchmark.run_chunks') as call, contextlib.redirect_stdout(io.StringIO()) as output:
                await main(args)
            call.assert_not_called()
            self.assertFalse(args.output.exists())
            self.assertNotIn('never-log-this', output.getvalue())

    async def test_budget_rejects_before_any_paid_call(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.setup_run(folder); args.max_estimated_usd = .0000001
            with patch('stt_provider_benchmark.credentials', return_value={'OPENROUTER_API_KEY': 'test'}), \
                    patch('stt_provider_benchmark.run_chunks') as call, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'MI-BENCH-014'):
                    await main(args)
            call.assert_not_called()

    async def test_failure_stops_provider_and_resume_does_not_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.setup_run(folder)
            with patch('stt_provider_benchmark.credentials', return_value={'OPENROUTER_API_KEY': 'never-log-this'}), \
                    patch('stt_provider_benchmark.OpenRouterTranscriptionService'), \
                    patch('stt_provider_benchmark.run_chunks', side_effect=BenchmarkError('private provider body')) as call, \
                    contextlib.redirect_stdout(io.StringIO()):
                await main(args)
                await main(args)
            self.assertEqual(call.call_count, 1)
            saved = args.output.read_text()
            self.assertNotIn('private provider body', saved)
            self.assertNotIn('never-log-this', saved)
            state = json.loads(saved)
            self.assertEqual(len(state['reservations']), 1)
            self.assertEqual(state['summary']['openrouter_chunks']['failed_runs'], 1)

    async def test_reserved_but_interrupted_call_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.setup_run(folder); args.repeats = 1
            with patch('stt_provider_benchmark.credentials', return_value={'OPENROUTER_API_KEY': 'test'}), \
                    patch('stt_provider_benchmark.OpenRouterTranscriptionService'), \
                    patch('stt_provider_benchmark.run_chunks', side_effect=BenchmarkError('test')) as call, \
                    contextlib.redirect_stdout(io.StringIO()):
                await main(args)
                state = json.loads(args.output.read_text()); state['rows'] = []
                write_json(args.output, state)
                await main(args)
            self.assertEqual(call.call_count, 1)

    async def test_changed_corpus_refuses_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.setup_run(folder)
            with patch('stt_provider_benchmark.credentials', return_value={}), contextlib.redirect_stdout(io.StringIO()):
                await main(args)
                args.manifest.write_text(args.manifest.read_text() + '\n')
                with self.assertRaisesRegex(ValueError, 'MI-BENCH-015'):
                    await main(args)


class ReportEvidenceTests(unittest.TestCase):
    def state(self):
        folder = Path(__file__).resolve().parents[2] / 'docs/benchmarks/stt-2026-10-19'
        return folder, {'rows': [], 'reservations': [],
            'missing_credentials': {p: spec.key for p, spec in PROVIDERS.items() if spec.key},
            'config': {'providers': {p: asdict(spec) for p, spec in PROVIDERS.items()},
                'repeats': 3, 'manifest_sha256': hashlib.sha256((folder / 'manifest.json').read_bytes()).hexdigest()}}

    def test_empty_run_does_not_claim_success_or_zero_error_rate(self):
        folder, state = self.state(); report = render(state, folder)
        self.assertIn('**Status: incomplete;', report)
        row = next(line for line in report.splitlines() if line.startswith('| Soniox stt-rt-v5 |'))
        self.assertIn('Needs API key', row)
        self.assertIn('| — | — / — |', row)
        self.assertTrue(row.endswith('| — |'))

    def test_incomplete_attempts_do_not_reduce_expected_corpus_size(self):
        folder, state = self.state()
        state['missing_credentials'] = {}
        state['reservations'] = [{'provider': 'aws', 'repeat': 0, 'clip': 'plain-us', 'reserved_usd': .001}]
        report = render(state, folder)
        self.assertIn('0/30 runs; not_run', report)
        self.assertIn('1 interrupted reservations', report)

    def test_report_rejects_different_corpus_instead_of_mixing_results(self):
        folder, state = self.state(); state['config']['manifest_sha256'] = 'different'
        with self.assertRaisesRegex(ValueError, 'MI-BENCH-020'):
            render(state, folder)

    def test_current_aws_report_cost_has_no_historical_minimum(self):
        rows = [{'provider': 'aws', 'status': 'measured', 'duration_s': 1.1, 'cost_usd': .0015}]
        self.assertAlmostEqual(cost_for_report('aws', rows), .0002)
        self.assertIsNone(cost_for_report('soniox', rows))


class SocketLifecycleTests(unittest.IsolatedAsyncioTestCase):
    class Socket:
        def __init__(self, provider, confirm=True):
            self.provider = provider; self.confirm = confirm
            self.queue = asyncio.Queue(); self.closed = False; self.turns = 0
            if provider == 'assemblyai':
                self.queue.put_nowait(json.dumps({'type': 'Begin',
                    'configuration': {'model': 'universal-streaming-english'}}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def recv(self):
            return await self.queue.get()

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.recv()

        async def send(self, message):
            if isinstance(message, bytes):
                return
            if message == '':
                if self.confirm:
                    self.queue.put_nowait(json.dumps({'tokens': [{'text': 'hello', 'is_final': True}], 'finished': True}))
                return
            data = json.loads(message)
            if data.get('type') == 'session.update':
                self.queue.put_nowait(json.dumps({'type': 'session.updated'}))
            if data.get('type') == 'input_audio_buffer.commit':
                self.turns += 1; item = f'turn{self.turns}'
                self.queue.put_nowait(json.dumps({'type': 'input_audio_buffer.committed',
                    'item_id': item, 'previous_item_id': None if self.turns == 1 else f'turn{self.turns - 1}'}))
                self.queue.put_nowait(json.dumps({'type': 'conversation.item.input_audio_transcription.completed',
                    'item_id': item, 'transcript': 'hello'}))
            if data.get('type') == 'Terminate' and self.confirm:
                self.queue.put_nowait(json.dumps({'type': 'Turn', 'end_of_turn': True,
                    'turn_order': 0, 'transcript': 'hello'}))
                self.queue.put_nowait(json.dumps({'type': 'Termination', 'session_duration_seconds': .2}))
            if data.get('type') == 'CloseStream' and self.confirm:
                self.queue.put_nowait(json.dumps({'type': 'Results', 'is_final': True,
                    'start': 0, 'channel': {'alternatives': [{'transcript': 'hello'}]}}))
                self.queue.put_nowait(json.dumps({'type': 'Metadata', 'duration': .02}))

    async def test_each_protocol_drains_final_text_and_releases_connection(self):
        for provider in ['soniox', 'assemblyai', 'deepgram', 'openai_turns', 'openai_live']:
            with self.subTest(provider=provider):
                socket = self.Socket(provider)
                with patch('stt_provider_benchmark.connect', return_value=socket):
                    events, _ = await run_socket(provider, PROVIDERS[provider], 'fake-unit-test-key', bytes(6400))
                self.assertTrue(socket.closed)
                self.assertEqual(' '.join(e['text'] for e in events if e['final']), 'hello')

    async def test_missing_final_confirmation_times_out_and_releases_connection(self):
        socket = self.Socket('soniox', confirm=False)
        with patch('stt_provider_benchmark.connect', return_value=socket), patch('stt_provider_benchmark.DRAIN_SECONDS', .02):
            with self.assertRaises(TimeoutError):
                await run_socket('soniox', PROVIDERS['soniox'], 'fake-unit-test-key', bytes(640))
        self.assertTrue(socket.closed)


if __name__ == '__main__':
    unittest.main()
