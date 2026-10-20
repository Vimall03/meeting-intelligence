"""Compare OpenRouter STT models using only OPENROUTER_API_KEY.

These are three-second completed-audio requests, not persistent streaming.
Uses the unchanged production transcription service and existing scorer.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import httpx

from app.services.openrouter_stt_service import OpenRouterTranscriptionService
from stt_provider_benchmark import (
    BenchmarkError, credentials, read_audio, run_chunks, safe_failure,
    score_events, write_json,
)
from stt_compare import percentile


# Unit is explicit: Microsoft publishes an hourly prompt price in this catalog;
# these other models publish per-second prices. Never infer units by magnitude.
MODELS = {
    'gpt_chunks': ('openai/gpt-transcribe', 'second'),
    'deepgram_chunks': ('deepgram/nova-3', 'second'),
    'assemblyai_chunks': ('assemblyai/universal-3-6-pro', 'second'),
    'voxtral_chunks': ('mistralai/voxtral-mini-transcribe', 'second'),
    'mai_chunks': ('microsoft/mai-transcribe-2', 'hour'),
    'parakeet_chunks': ('nvidia/parakeet-tdt-0.6b-v3', 'second'),
    'whisper_turbo_chunks': ('openai/whisper-large-v3-turbo', 'second'),
}


class StopOnProviderFailure:
    """Stop on an unusable provider instead of repeating rejected requests."""
    def __init__(self, service, duration):
        self.service = service; self.duration = duration; self.calls = 0

    def transcribe(self, path):
        duration = min(3, self.duration - self.calls * 3); self.calls += 1
        try:
            return self.service.transcribe(path)
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status == 400 and duration < .25:
                # Preserve the original comparison's rejected tiny-tail behaviour.
                raise
            failure = BenchmarkError('MI-BENCH-OR-001: transcription request failed')
            failure.response = getattr(exc, 'response', None)
            raise failure from None


def aggregate(rows):
    measured = [r for r in rows if r['status'] == 'measured']
    words = sum(r['metrics']['reference_words'] for r in measured)
    errors = sum(r['metrics'][key] for r in measured for key in ('substitutions', 'deletions', 'insertions'))
    lags = [v['lag_s'] for r in measured for v in r['metrics']['indexed_final_lags']]
    return {'measured_runs': len(measured), 'failed_runs': sum(r['status'] == 'failed' for r in rows),
        'reference_words': words, 'errors': errors, 'wer': errors / words if words else None,
        'final_latency_p50_s': percentile(lags, .5), 'final_latency_p95_s': percentile(lags, .95),
        'latency_coverage': len(lags), 'utterance_count': sum(r['metrics']['utterance_count'] for r in measured),
        'request_failures': sum(len(r['details']['failures']) for r in measured),
        'requests': sum(r['details']['requests'] for r in measured),
        'test_cost_usd': sum(r['cost_usd'] for r in measured),
        'cost_bases': sorted({r['cost_basis'] for r in measured})}


def selected_rates(catalog):
    by_id = {m['id']: m for m in catalog['data']}
    output = {}
    for name, (slug, unit) in MODELS.items():
        item = by_id[slug]; pricing = item['pricing']
        rate = float(pricing['prompt']) * (3600 if unit == 'second' else 1)
        if float(pricing['completion']) or not 0 < rate < 2:
            raise ValueError('MI-BENCH-OR-002: unexpected pricing; review before paid calls')
        output[name] = {'model': slug, 'name': item['name'], 'hourly_usd': rate,
            'catalog_price_unit': unit, 'catalog_pricing': pricing,
            'source': 'https://openrouter.ai/' + slug}
    return output


async def main(args):
    if not 1 <= args.repeats <= 3 or not 0 < args.max_estimated_usd <= 5:
        raise ValueError('MI-BENCH-OR-003: use at most three repeats and $5 estimate limit')
    manifest = json.loads(args.manifest.read_text()); clips = manifest['clips']
    if not 1 <= len(clips) <= 100 or sum(c['duration_s'] for c in clips) * args.repeats > 3600:
        raise ValueError('MI-BENCH-OR-004: maximum one replayed hour per model')
    for clip in clips:
        read_audio(args.manifest.resolve().parent, clip)
    models = selected_rates(json.loads(args.catalog.read_text()))
    reservations_per_model = {name: sum(math.ceil(c['duration_s'] / 3) * 15 / 3600 * spec['hourly_usd']
        for c in clips) * args.repeats for name, spec in models.items()}
    estimate = sum(reservations_per_model.values())
    print(json.dumps({'execute': args.execute, 'models': list(models),
        'base_price_expected_usd': sum(s['hourly_usd'] for s in models.values()) * sum(c['duration_s'] for c in clips) * args.repeats / 3600,
        'conservative_reservation_usd': estimate, 'estimate_limit_usd': args.max_estimated_usd}), flush=True)
    if estimate > args.max_estimated_usd:
        raise ValueError('MI-BENCH-OR-005: reservation exceeds estimate limit')
    if not args.execute:
        return
    values = credentials()
    if not values.get('OPENROUTER_API_KEY'):
        raise ValueError('MI-BENCH-OR-006: configure OPENROUTER_API_KEY locally')
    config = {'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        'catalog_sha256': hashlib.sha256(args.catalog.read_bytes()).hexdigest(),
        'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'helper_sha256': hashlib.sha256(Path(__file__).with_name('stt_provider_benchmark.py').read_bytes()).hexdigest(),
        'models': models, 'repeats': args.repeats, 'clip_count': len(clips),
        'transport': 'independent_3_second_webm_to_wav_requests',
        'endpoint': 'https://openrouter.ai/api/v1/audio/transcriptions',
        'maximum_concurrent_models': len(models), 'max_estimated_usd': args.max_estimated_usd}
    state = {'schema': 1, 'config': config, 'created_at': datetime.now(timezone.utc).isoformat(),
        'rows': [], 'reservations': []}
    if args.output.exists():
        state = json.loads(args.output.read_text())
        if state['config'] != config:
            raise ValueError('MI-BENCH-OR-007: resume requires unchanged inputs and code')
    rows = state['rows']; reservations = state['reservations']
    reserved = {(r['provider'], r['repeat'], r['clip']) for r in reservations}
    failed_models = {r['provider'] for r in rows if r['status'] == 'failed'}
    interrupted_models = {r['provider'] for r in reservations if not any(
        (v['provider'], v['repeat'], v['clip']) == (r['provider'], r['repeat'], r['clip']) for v in rows)}

    def save():
        state['summary'] = {name: aggregate([r for r in rows if r['provider'] == name]) for name in models}
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        write_json(args.output, state)

    async def run_model(name, spec):
        if name in failed_models or name in interrupted_models:
            return
        service = OpenRouterTranscriptionService(values['OPENROUTER_API_KEY'], model=spec['model'])
        try:
            for repeat in range(args.repeats):
                for clip in clips:
                    identity = (name, repeat, clip['id'])
                    if identity in reserved:
                        continue
                    reservation = math.ceil(clip['duration_s'] / 3) * 15 / 3600 * spec['hourly_usd']
                    if sum(v['reserved_usd'] for v in reservations) + reservation > args.max_estimated_usd:
                        raise ValueError('MI-BENCH-OR-008: local estimate guard exhausted')
                    row = {'provider': name, 'repeat': repeat, 'clip': clip['id'],
                        'duration_s': clip['duration_s'], 'category': clip['category'], 'reserved_usd': reservation}
                    reservations.append(dict(row)); reserved.add(identity); save()
                    pcm = read_audio(args.manifest.resolve().parent, clip)
                    try:
                        # Each model has an independent replay clock/event loop.
                        # Its pre-encoding cannot block another model's receiver.
                        proxy = StopOnProviderFailure(service, clip['duration_s'])
                        events, details = await asyncio.to_thread(lambda: asyncio.run(run_chunks(pcm, proxy)))
                        costs = details['provider_usage']['costs_usd']
                        reported = sum(c for c in costs if type(c) in (int, float))
                        missing = [i for i, c in enumerate(costs) if type(c) not in (int, float)]
                        estimated = sum(min(3, clip['duration_s'] - i * 3) / 3600 * spec['hourly_usd'] for i in missing)
                        row.update(status='measured', events=events, details=details,
                            metrics=score_events(clip, events), cost_usd=reported + estimated,
                            reported_cost_usd=reported, estimated_unreported_cost_usd=estimated,
                            cost_basis='provider_reported' if not missing else 'provider_reported_plus_failed_window_estimate')
                        rows.append(row); save()
                        print(json.dumps({k: row[k] for k in ('provider', 'repeat', 'clip', 'status')}), flush=True)
                    except Exception as exc:
                        row.update(status='failed', failure=safe_failure(exc))
                        rows.append(row); save()
                        print(json.dumps({k: row[k] for k in ('provider', 'repeat', 'clip', 'status')}), flush=True)
                        return
        finally:
            service.close()
    await asyncio.gather(*(run_model(name, spec) for name, spec in models.items()))
    save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--max-estimated-usd', type=float, default=1)
    parser.add_argument('--execute', action='store_true')
    asyncio.run(main(parser.parse_args()))
