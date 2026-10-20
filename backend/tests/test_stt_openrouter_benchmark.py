from types import SimpleNamespace
import json
from pathlib import Path
import unittest

import httpx

from stt_openrouter_benchmark import MODELS, StopOnProviderFailure, aggregate, selected_rates
from stt_provider_benchmark import BenchmarkError, safe_failure
from stt_openrouter_report import pairwise, render


class OpenRouterBenchmarkTests(unittest.TestCase):
    def catalog(self):
        return {'data': [{'id': slug, 'name': slug, 'pricing': {
            'prompt': '.1' if unit == 'hour' else '.00001', 'completion': '0'}}
            for slug, unit in MODELS.values()]}

    def test_catalog_hourly_and_second_prices_are_distinguished(self):
        rates = selected_rates(self.catalog())
        self.assertAlmostEqual(rates['mai_chunks']['hourly_usd'], .1)
        self.assertAlmostEqual(rates['voxtral_chunks']['hourly_usd'], .036)

    def test_missing_model_does_not_fall_back_to_another_model(self):
        with self.assertRaises(KeyError):
            selected_rates({'data': []})

    def test_unexpected_pricing_stops_before_paid_calls(self):
        catalog = self.catalog(); catalog['data'][0]['pricing']['completion'] = '.1'
        with self.assertRaisesRegex(ValueError, 'MI-BENCH-OR-002'):
            selected_rates(catalog)

    def failed_service(self, status):
        def transcribe(path):
            request = httpx.Request('POST', 'https://openrouter.ai/api/v1/audio/transcriptions')
            raise httpx.HTTPStatusError('private-provider-body', request=request,
                response=httpx.Response(status, request=request))
        return SimpleNamespace(transcribe=transcribe)

    def test_auth_and_balance_failures_stop_without_exposing_body(self):
        for status in [401, 402, 403, 429, 503]:
            proxy = StopOnProviderFailure(self.failed_service(status), 15)
            with self.assertRaises(BenchmarkError) as caught:
                proxy.transcribe('unit-test.webm')
            self.assertEqual(safe_failure(caught.exception)['http_status'], status)
            self.assertNotIn('private-provider-body', str(caught.exception))

    def test_bad_regular_window_stops_but_known_tiny_tail_is_recorded(self):
        with self.assertRaises(BenchmarkError):
            StopOnProviderFailure(self.failed_service(400), 3).transcribe('unit-test.webm')
        with self.assertRaises(httpx.HTTPStatusError):
            StopOnProviderFailure(self.failed_service(400), .09).transcribe('unit-test.webm')

    def test_no_measurement_never_becomes_zero_wer(self):
        summary = aggregate([])
        self.assertIsNone(summary['wer'])
        self.assertIsNone(summary['final_latency_p50_s'])
        self.assertEqual(summary['measured_runs'], 0)


class OpenRouterReportTests(unittest.TestCase):
    def fixtures(self):
        folder = Path(__file__).resolve().parents[2] / 'docs/benchmarks'
        earlier = json.loads((folder / 'stt-providers-2026-10-20/results.json').read_text())
        legacy = json.loads((folder / 'stt-2026-10-19/results.json').read_text())
        offline = json.loads((folder / 'stt-2026-10-19/whole-clip-control.json').read_text())
        state = {'rows': [], 'config': {'manifest_sha256': earlier['config']['manifest_sha256'],
            'repeats': 3, 'clip_count': 10, 'models': {'gpt_chunks': {'name': 'GPT control',
                'hourly_usd': .27, 'source': 'https://openrouter.ai/openai/gpt-transcribe'}}}}
        return state, earlier, legacy, offline

    def test_empty_run_is_incomplete_and_not_zero_wer(self):
        report = render(*self.fixtures())
        self.assertIn('run incomplete', report)
        row = next(line for line in report.splitlines() if line.startswith('| GPT control via'))
        self.assertIn('| 0/30 | — | — / — |', row)

    def test_compares_same_audio_or_refuses_report(self):
        state, earlier, legacy, offline = self.fixtures()
        state['config']['manifest_sha256'] = 'other'
        with self.assertRaisesRegex(ValueError, 'MI-BENCH-OR-009'):
            render(state, earlier, legacy, offline)

    def test_paired_latency_only_uses_common_final_word_matches(self):
        rows = [
            {'provider': 'gpt_chunks', 'status': 'measured', 'repeat': 0, 'clip': 'x', 'metrics':
                {'indexed_final_lags': [{'utterance_index': 0, 'lag_s': 1}, {'utterance_index': 1, 'lag_s': 2}]}},
            {'provider': 'candidate', 'status': 'measured', 'repeat': 0, 'clip': 'x', 'metrics':
                {'indexed_final_lags': [{'utterance_index': 1, 'lag_s': 3}]}}]
        pair = pairwise(rows, 'candidate')
        self.assertEqual(pair['candidate']['samples'], 1)
        self.assertEqual(pair['gpt_control']['p50_s'], 2)

    def test_report_does_not_label_openrouter_as_continuous_streaming(self):
        report = render(*self.fixtures())
        self.assertIn('not persistent streaming', report)
        self.assertIn('whole-clip GPT offline control', report)
        self.assertIn('not the $0.15/hour Universal-Streaming model', report)


if __name__ == '__main__':
    unittest.main()
