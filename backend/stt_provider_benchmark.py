"""Bounded, opt-in STT experiment. Never imported by the meeting application.

Reuses the original corpus and normalizer. Default invocation only prints a plan;
--execute permits paid calls. No retries or production settings are changed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlencode
import wave

from dotenv import dotenv_values
import httpx
import numpy as np
from websockets.asyncio.client import connect

from stt_compare import alignment, percentile, tokens
from app.services.openrouter_stt_service import OpenRouterTranscriptionService
from app.services.streaming_stt import AmazonStreamingTranscription


@dataclass(frozen=True)
class Provider:
    model: str
    key: str | None
    hourly_usd: float
    transport: str
    pricing_url: str
    note: str = ''


PROVIDERS = {
    'openrouter_chunks': Provider('openai/gpt-transcribe', 'OPENROUTER_API_KEY', .27,
        'independent_3_second_webm', 'https://openrouter.ai/openai/gpt-transcribe'),
    'aws': Provider('Amazon Transcribe Standard, en-US, medium stabilization', None, .36,
        'persistent_pcm', 'https://aws.amazon.com/transcribe/pricing/',
        'Mumbai rate from saved AWS Price List; recheck before interpreting estimates.'),
    'soniox': Provider('stt-rt-v5', 'SONIOX_API_KEY', .12, 'persistent_pcm',
        'https://soniox.com/pricing', 'Approximate token-based rate; not an invoice.'),
    'assemblyai': Provider('universal-streaming-english', 'ASSEMBLYAI_API_KEY', .15,
        'persistent_pcm', 'https://www.assemblyai.com/pricing',
        'Connected session duration, including silence; no paid add-ons enabled.'),
    'deepgram': Provider('nova-3', 'DEEPGRAM_API_KEY', .288, 'persistent_pcm',
        'https://deepgram.com/pricing', 'Monolingual promotional rate; regular $0.462/hour. No paid add-ons.'),
    'openai_turns': Provider('gpt-transcribe', 'OPENAI_API_KEY', .27,
        'persistent_pcm_client_vad', 'https://developers.openai.com/api/docs/pricing'),
    'openai_live': Provider('gpt-live-transcribe', 'OPENAI_API_KEY', 1.02,
        'persistent_pcm_client_vad', 'https://developers.openai.com/api/docs/pricing',
        'Low delay; client energy VAD, not reference utterance boundaries.'),
}
DRAIN_SECONDS = 20
SETUP_SECONDS = 15
ROOT = Path(__file__).resolve().parent


class BenchmarkError(RuntimeError):
    """Stable local diagnostic; never save provider bodies or credentials."""


def safe_failure(exc):
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    return {'code': 'MI-BENCH-001', 'type': type(exc).__name__, 'http_status': status}


def credentials():
    # Process variables take precedence; do not print any values.
    return {**dotenv_values(ROOT / 'app/.env'), **dotenv_values(ROOT / '.env'), **os.environ}


def read_audio(folder, clip):
    path = (folder / clip['audio']).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError('MI-BENCH-002: audio must be inside corpus folder')
    if hashlib.sha256(path.read_bytes()).hexdigest() != clip['sha256']:
        raise ValueError('MI-BENCH-003: corpus audio hash mismatch')
    with wave.open(str(path)) as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError('MI-BENCH-004: expected mono PCM16 at 16 kHz')
        pcm = wav.readframes(wav.getnframes())
    if not 0 < len(pcm) / 32000 <= 120:
        raise ValueError('MI-BENCH-005: clip must be between zero and 120 seconds')
    if abs(len(pcm) / 32000 - clip['duration_s']) > 1 / 16000:
        raise ValueError('MI-BENCH-006: manifest duration mismatch')
    return pcm


def score_events(clip, events):
    """Keep subwords joined and preserve each normalized word's availability.

Final events have provider-defined stable IDs and audio ordering. Soniox sends
subwords, so inserting spaces between response packets would corrupt its WER.
OpenAI final turns can arrive out of order, so arrival order is not text order.
"""
    finals = {}
    for event in events:
        if event['final'] and event['id'] not in finals:
            finals[event['id']] = event
    ordered = sorted(finals.values(), key=lambda e: e['order'])
    text = ''; hypothesis = []; available = []
    for event in ordered:
        text += event.get('join', ' ') + event['text']
        current = tokens(text)
        common = 0
        while common < min(len(current), len(hypothesis)) and current[common] == hypothesis[common]:
            common += 1
        # A subword becomes a complete word only when its last piece arrives.
        prior_time = max(available[common:], default=0)
        available = available[:common] + [max(prior_time, event['received_s'])] * (len(current) - common)
        hypothesis = current
    reference = [word for u in clip['utterances'] for word in tokens(u['text'])]
    stats, matches = alignment(reference, hypothesis)
    indexed = []; end_index = 0
    for index, utterance in enumerate(clip['utterances']):
        end_index += len(tokens(utterance['text']))
        if end_index - 1 in matches:
            indexed.append({'utterance_index': index,
                'lag_s': available[matches[end_index - 1]] - utterance['end']})
    partials = [e['received_s'] - clip['utterances'][0]['start']
        for e in events if not e['final'] and e['text'].strip()]
    return {**stats, 'final_text': text.strip(), 'indexed_final_lags': indexed,
        'latency_coverage': len(indexed), 'utterance_count': len(clip['utterances']),
        'first_partial_after_speech_s': min(partials, default=None)}


class EventParser:
    """Pure provider protocol normalization, exercised without paid calls."""
    def __init__(self, provider):
        self.provider = provider
        self.sequence = 0
        self.usage = {}
        self.previous_items = {}
        self.done = False

    def parse(self, data, received_s):
        provider = self.provider; kind = data.get('type'); output = []
        if data.get('error_code') or kind == 'Error' or kind == 'error' or kind == 'conversation.item.input_audio_transcription.failed':
            raise BenchmarkError('MI-BENCH-007: provider rejected benchmark session')

        def emit(text, final, identifier, order, join=' '):
            if text:
                output.append({'text': text, 'final': bool(final), 'id': str(identifier),
                    'order': order, 'join': join, 'received_s': received_s})

        if provider == 'soniox':
            for token in data.get('tokens', []):
                text = token.get('text', '')
                if text in ('<end>', '<fin>') or token.get('translation_status') == 'translation':
                    continue
                self.sequence += 1
                emit(text, token.get('is_final', False), self.sequence, self.sequence, '')
            self.done = bool(data.get('finished'))
        elif provider == 'assemblyai':
            if kind == 'Turn':
                emit(data.get('transcript', ''), data.get('end_of_turn'),
                    data['turn_order'], data['turn_order'])
            if kind == 'Termination':
                self.usage = {k: data[k] for k in ('audio_duration_seconds', 'session_duration_seconds') if k in data}
                self.done = True
        elif provider == 'deepgram':
            if kind == 'Results':
                alternatives = data.get('channel', {}).get('alternatives', [])
                if alternatives:
                    emit(alternatives[0].get('transcript', ''), data.get('is_final'),
                        data.get('start', 0), data.get('start', 0))
            if kind == 'Metadata' and 'duration' in data:
                self.usage = {'audio_duration_seconds': data['duration']}
                self.done = True
        else:
            if kind == 'input_audio_buffer.committed':
                self.previous_items[data['item_id']] = data.get('previous_item_id')
            if kind in ('conversation.item.input_audio_transcription.completed', 'conversation.item.input_audio_transcription.delta'):
                # Resolve audio order after reception from the commit item chain.
                item = data['item_id']
                emit(data.get('transcript', data.get('delta', '')),
                    kind.endswith('.completed'), item, item)
                if 'usage' in data:
                    self.usage.setdefault('turns', []).append(data['usage'])
        return output

    def order_openai(self, events):
        if not self.provider.startswith('openai_'):
            return
        def rank(item):
            seen = set()
            while item in self.previous_items:
                if item in seen:
                    raise BenchmarkError('MI-BENCH-008: invalid item chain')
                seen.add(item); item = self.previous_items[item]
            return len(seen)
        for event in events:
            if event['id'] not in self.previous_items:
                raise BenchmarkError('MI-BENCH-009: missing committed turn ordering')
            event['order'] = rank(event['id'])


class EnergyVAD:
    """Causal 20 ms energy detector; no reference timestamps or words are used.

Noise floor calibrated on initial 400 ms; commit after 400 ms of silence.
This simple baseline is part of the measured OpenAI configuration, not an
accuracy claim about a production VAD or the provider alone.
"""
    def __init__(self):
        self.noise = []; self.active = False; self.quiet = 0; self.buffered = 0

    def consume(self, frame):
        commit = False
        for offset in range(0, len(frame), 640):
            samples = np.frombuffer(frame[offset:offset + 640], dtype='<i2').astype(float)
            if not len(samples):
                continue
            rms = float(np.sqrt(np.mean(samples * samples)))
            self.buffered += len(samples)
            if len(self.noise) < 20:
                self.noise.append(rms)
            threshold = max(150, float(np.median(self.noise)) * 2.5)
            if rms > threshold:
                self.active = True; self.quiet = 0
            elif self.active:
                self.quiet += len(samples)
                if self.quiet >= 6400:
                    commit = True; self.active = False; self.quiet = 0
        if commit:
            self.buffered = 0
        return commit


async def run_socket(provider, spec, key, pcm):
    parser = EventParser(provider); events = []; started = time.perf_counter()
    resampled = None
    if provider.startswith('openai_'):
        resampled = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 's16le',
            '-ar', '16000', '-ac', '1', '-i', 'pipe:0', '-ar', '24000',
            '-f', 's16le', 'pipe:1'], input=pcm, capture_output=True, check=True).stdout
    if provider == 'soniox':
        url = 'wss://stt-rt.soniox.com/transcribe-websocket'
        headers = {'Authorization': 'Bearer ' + key}
        config = {'model': spec.model, 'audio_format': 's16le', 'sample_rate': 16000,
            'num_channels': 1, 'language_hints': ['en'], 'enable_endpoint_detection': True}
    elif provider == 'assemblyai':
        url = 'wss://streaming.assemblyai.com/v3/ws?' + urlencode({
            'sample_rate': 16000, 'encoding': 'pcm_s16le', 'speech_model': spec.model,
            'format_turns': 'false', 'inactivity_timeout': 10})
        headers = {'Authorization': key}; config = None
    elif provider == 'deepgram':
        url = 'wss://api.deepgram.com/v1/listen?' + urlencode({'model': spec.model,
            'language': 'en-US', 'encoding': 'linear16', 'sample_rate': 16000,
            'channels': 1, 'interim_results': 'true', 'punctuate': 'true', 'endpointing': 300})
        headers = {'Authorization': 'Token ' + key}; config = None
    else:
        url = 'wss://api.openai.com/v1/realtime?intent=transcription'
        headers = {'Authorization': 'Bearer ' + key}
        transcription = {'model': spec.model}
        if provider == 'openai_live':
            transcription.update(languages=['en'], delay='low')
        else:
            transcription['language'] = 'en'
        config = {'type': 'session.update', 'session': {'type': 'transcription',
            'audio': {'input': {'format': {'type': 'audio/pcm', 'rate': 24000},
                'transcription': transcription, 'turn_detection': None}}}}

    async with connect(url, additional_headers=headers, open_timeout=SETUP_SECONDS,
            close_timeout=2, max_size=2 ** 22) as ws:
        if config:
            await ws.send(json.dumps(config))
        if provider == 'assemblyai' or provider.startswith('openai_'):
            expected = 'Begin' if provider == 'assemblyai' else 'session.updated'
            async with asyncio.timeout(SETUP_SECONDS):
                while True:
                    data = json.loads(await ws.recv())
                    parser.parse(data, 0)
                    if data.get('type') == expected:
                        if provider == 'assemblyai' and data.get('configuration', {}).get('model') != spec.model:
                            raise BenchmarkError('MI-BENCH-017: provider model echo mismatch')
                        break
        setup_s = time.perf_counter() - started
        began = time.perf_counter(); sent_done = asyncio.Event(); final_done = asyncio.Event()
        commits = 0; finals = 0

        async def receive():
            nonlocal finals
            async for message in ws:
                data = json.loads(message)
                parsed = parser.parse(data, time.perf_counter() - began)
                events.extend(parsed)
                finals += sum(e['final'] for e in parsed)
                if parser.done or (provider.startswith('openai_') and sent_done.is_set() and finals >= commits):
                    final_done.set(); return
            if not parser.done and not (provider.startswith('openai_') and finals >= commits):
                raise BenchmarkError('MI-BENCH-010: session closed before final confirmation')

        async def send():
            nonlocal commits
            vad = EnergyVAD()
            for offset in range(0, len(pcm), 6400):
                frame = pcm[offset:offset + 6400]
                await asyncio.sleep(max(0, began + (offset + len(frame)) / 32000 - time.perf_counter()))
                if provider.startswith('openai_'):
                    target = resampled[offset * 3 // 2:(offset + len(frame)) * 3 // 2]
                    await ws.send(json.dumps({'type': 'input_audio_buffer.append',
                        'audio': base64.b64encode(target).decode('ascii')}))
                    if vad.consume(frame):
                        commits += 1
                        await ws.send(json.dumps({'type': 'input_audio_buffer.commit'}))
                else:
                    await ws.send(frame)
            if provider.startswith('openai_'):
                if vad.buffered >= 1600:
                    commits += 1
                    await ws.send(json.dumps({'type': 'input_audio_buffer.commit'}))
            elif provider == 'soniox':
                await ws.send('')
            elif provider == 'assemblyai':
                await ws.send(json.dumps({'type': 'Terminate'}))
            else:
                await ws.send(json.dumps({'type': 'CloseStream'}))
            sent_done.set()
            if provider.startswith('openai_') and finals >= commits:
                final_done.set()

        sender = asyncio.create_task(send()); receiver = asyncio.create_task(receive())
        terminal = asyncio.create_task(final_done.wait())
        tasks = [sender, receiver, terminal]
        try:
            async with asyncio.timeout(len(pcm) / 32000 + DRAIN_SECONDS):
                # If reception fails while sending, cancel the sender immediately.
                done, _ = await asyncio.wait([sender, receiver], return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    await receiver
                    if not sender.done():
                        raise BenchmarkError('MI-BENCH-018: early provider termination')
                await sender
                await asyncio.wait([receiver, terminal], return_when=asyncio.FIRST_COMPLETED)
                if receiver.done():
                    await receiver
                if not final_done.is_set():
                    raise BenchmarkError('MI-BENCH-019: missing terminal response')
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        parser.order_openai(events)
        return events, {'setup_s': setup_s, 'connected_s': time.perf_counter() - started,
            'provider_usage': parser.usage, 'committed_turns': commits}


async def run_aws(pcm, region, profile):
    stream = AmazonStreamingTranscription(region=region, profile=profile)
    events = []; started = time.perf_counter()
    await stream.start()
    setup_s = time.perf_counter() - started; began = time.perf_counter()
    async def send():
        for offset in range(0, len(pcm), 6400):
            frame = pcm[offset:offset + 6400]
            await asyncio.sleep(max(0, began + (offset + len(frame)) / 32000 - time.perf_counter()))
            await stream.send(frame)
        await stream.finish()
    async def receive():
        async for update in stream.updates():
            events.append({'id': update.result_id, 'text': update.text, 'final': update.final,
                'order': update.start, 'received_s': time.perf_counter() - began})
    tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), len(pcm) / 32000 + DRAIN_SECONDS)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await stream.finish()
    return events, {'setup_s': setup_s, 'provider_usage': {}}


async def run_chunks(pcm, service):
    events = []; usage = []; failures = []
    with tempfile.TemporaryDirectory(prefix='mi-benchmark-') as temp:
        windows = []
        for offset in range(0, len(pcm), 96000):
            frame = pcm[offset:offset + 96000]; path = Path(temp) / f'{offset}.webm'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 's16le',
                '-ar', '16000', '-ac', '1', '-i', 'pipe:0', '-c:a', 'libopus', str(path)],
                input=frame, capture_output=True, check=True)
            windows.append(((offset + len(frame)) / 32000, path))
        began = time.perf_counter()
        for index, (end, path) in enumerate(windows):
            await asyncio.sleep(max(0, began + end - time.perf_counter()))
            try:
                result = await asyncio.to_thread(service.transcribe, str(path))
                events.append({'id': str(index), 'order': index, 'final': True,
                    'text': ' '.join(s['text'] for s in result['segments']),
                    'received_s': time.perf_counter() - began})
                usage.append(result.get('usage_cost_usd'))
            except httpx.HTTPError as exc:
                failures.append(safe_failure(exc)); usage.append(None)
        return events, {'provider_usage': {'costs_usd': usage}, 'failures': failures,
            'requests': len(windows), 'setup_s': None}


def measured_cost(provider, spec, duration, details):
    usage = details.get('provider_usage', {})
    if provider == 'openrouter_chunks':
        costs = usage.get('costs_usd', [])
        if costs and all(type(c) in (int, float) and c >= 0 for c in costs):
            return sum(costs), 'provider_reported'
    if provider == 'assemblyai' and 'session_duration_seconds' in usage:
        return usage['session_duration_seconds'] / 3600 * spec.hourly_usd, 'reported_session_duration_x_list_rate'
    if provider == 'deepgram' and 'audio_duration_seconds' in usage:
        return usage['audio_duration_seconds'] / 3600 * spec.hourly_usd, 'reported_audio_duration_x_promotional_rate'
    if provider == 'aws':
        # Conservative historical minimum retained until billing is reconciled.
        return max(15, math.ceil(duration)) / 3600 * spec.hourly_usd, 'historical_regional_rate_and_minimum_estimate'
    return duration / 3600 * spec.hourly_usd, 'published_rate_estimate_not_invoice'


def summarise(rows, selected, repeats, clip_count):
    result = {}
    for provider in selected:
        data = [r for r in rows if r['provider'] == provider and r['status'] == 'measured']
        failed = [r for r in rows if r['provider'] == provider and r['status'] == 'failed']
        words = sum(r['metrics']['reference_words'] for r in data)
        errors = sum(r['metrics'][k] for r in data for k in ('substitutions', 'deletions', 'insertions'))
        lags = [v['lag_s'] for r in data for v in r['metrics']['indexed_final_lags']]
        partials = [r['metrics']['first_partial_after_speech_s'] for r in data
            if r['metrics']['first_partial_after_speech_s'] is not None]
        result[provider] = {'status': ('complete' if len(data) == repeats * clip_count
            else 'partial' if data else 'failed' if failed else 'not_run'),
            'measured_runs': len(data), 'failed_runs': len(failed),
            'reference_words': words, 'errors': errors, 'wer': errors / words if words else None,
            'final_latency_p50_s': percentile(lags, .5), 'final_latency_p95_s': percentile(lags, .95),
            'latency_coverage': len(lags), 'utterance_count': sum(r['metrics']['utterance_count'] for r in data),
            'first_partial_p50_s': percentile(partials, .5),
            'cost_usd': sum(r['cost_usd'] for r in data),
            'request_failures': sum(len(r['details'].get('failures', [])) for r in data),
            'unreconciled_failed_cost_reserve_usd': sum(r['reserved_usd'] for r in failed)}
    # Equal-coverage pairwise latencies; do not discard WER errors to look fast.
    for provider, summary in result.items():
        if provider == 'openrouter_chunks':
            continue
        paired = {provider: [], 'openrouter_chunks': []}
        baseline = {(r['repeat'], r['clip']): r for r in rows
            if r['provider'] == 'openrouter_chunks' and r['status'] == 'measured'}
        for row in rows:
            if row['provider'] != provider or row['status'] != 'measured':
                continue
            other = baseline.get((row['repeat'], row['clip']))
            if not other:
                continue
            a = {v['utterance_index']: v['lag_s'] for v in row['metrics']['indexed_final_lags']}
            b = {v['utterance_index']: v['lag_s'] for v in other['metrics']['indexed_final_lags']}
            for index in sorted(a.keys() & b.keys()):
                paired[provider].append(a[index]); paired['openrouter_chunks'].append(b[index])
        summary['paired_with_chunks'] = {p: {'samples': len(v), 'p50_s': percentile(v, .5),
            'p95_s': percentile(v, .95)} for p, v in paired.items()}
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n'); temp.replace(path)


async def main(args):
    manifest = json.loads(args.manifest.read_text()); folder = args.manifest.resolve().parent
    selected = list(PROVIDERS) if args.providers == 'all' else args.providers.split(',')
    if not selected or len(set(selected)) != len(selected) or any(p not in PROVIDERS for p in selected):
        raise ValueError('MI-BENCH-011: unknown or duplicate provider')
    if not 1 <= args.repeats <= 3 or not 0 < args.max_estimated_usd <= 5:
        raise ValueError('MI-BENCH-012: maximum three repeats and $5 estimate limit')
    clips = manifest['clips']
    if not 1 <= len(clips) <= 100 or sum(c['duration_s'] for c in clips) * args.repeats > 3600:
        raise ValueError('MI-BENCH-013: maximum one audio hour per provider')
    for clip in clips:
        read_audio(folder, clip)
    config = {'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        'providers': {p: asdict(PROVIDERS[p]) for p in selected}, 'repeats': args.repeats,
        'region': args.region, 'drain_seconds': DRAIN_SECONDS,
        'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'scorer_sha256': hashlib.sha256((ROOT / 'stt_compare.py').read_bytes()).hexdigest()}
    values = credentials()
    available = [p for p in selected if (values.get(PROVIDERS[p].key) if PROVIDERS[p].key else args.profile)]
    bound = sum((c['duration_s'] + DRAIN_SECONDS + SETUP_SECONDS) / 3600 * PROVIDERS[p].hourly_usd
        for c in clips for p in available) * args.repeats
    print(json.dumps({'mode': 'execute' if args.execute else 'plan_only',
        'available': available, 'missing': {p: PROVIDERS[p].key or 'AWS profile' for p in selected if p not in available},
        'audio_seconds_per_provider': sum(c['duration_s'] for c in clips) * args.repeats,
        'conservative_list_price_reservation_usd': round(bound, 5),
        'estimate_limit_usd': args.max_estimated_usd}), flush=True)
    if bound > args.max_estimated_usd:
        raise ValueError('MI-BENCH-014: estimated reservation exceeds limit')
    if not args.execute:
        return
    state = {'schema': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
        'config': config, 'rows': [], 'reservations': []}
    if args.output.exists():
        state = json.loads(args.output.read_text())
        if state['config'] != config:
            raise ValueError('MI-BENCH-015: resume requires identical corpus, code, models and repeats')
    rows = state['rows']; reservations = state['reservations']
    completed = {(r['provider'], r['repeat'], r['clip']) for r in rows}
    blocked = {r['provider'] for r in rows if r['status'] == 'failed'}
    # A crash after reservation must not cause a silent duplicate paid request.
    reserved = {(r['provider'], r['repeat'], r['clip']) for r in reservations}
    service = OpenRouterTranscriptionService(values['OPENROUTER_API_KEY']) if 'openrouter_chunks' in available else None

    async def run_reserved(row, clip, pcm):
        provider = row['provider']; spec = PROVIDERS[provider]
        try:
            if provider == 'openrouter_chunks':
                events, details = await run_chunks(pcm, service)
            else:
                operation = (run_aws(pcm, args.region, args.profile) if provider == 'aws'
                    else run_socket(provider, spec, values[spec.key], pcm))
                events, details = await asyncio.wait_for(operation,
                    clip['duration_s'] + DRAIN_SECONDS + SETUP_SECONDS)
            cost, basis = measured_cost(provider, spec, clip['duration_s'], details)
            row.update(status='measured', events=events, details=details,
                metrics=score_events(clip, events), cost_usd=cost, cost_basis=basis)
        except Exception as exc:
            row.update(status='failed', failure=safe_failure(exc)); blocked.add(provider)
        rows.append(row)
        state['summary'] = summarise(rows, selected, args.repeats, len(clips))
        write_json(args.output, state)
        print(json.dumps({k: row[k] for k in ('provider', 'repeat', 'clip', 'status')}), flush=True)

    try:
        for repeat in range(args.repeats):
            for clip in clips:
                pcm = read_audio(folder, clip)
                work = []
                for provider in available:
                    identity = (provider, repeat, clip['id'])
                    if identity in completed or identity in reserved or provider in blocked:
                        continue
                    spec = PROVIDERS[provider]
                    reservation = (clip['duration_s'] + DRAIN_SECONDS + SETUP_SECONDS) / 3600 * spec.hourly_usd
                    if sum(r['reserved_usd'] for r in reservations) + reservation > args.max_estimated_usd:
                        raise ValueError('MI-BENCH-016: budget reservation exhausted')
                    row = {'provider': provider, 'repeat': repeat, 'clip': clip['id'],
                        'category': clip['category'], 'duration_s': clip['duration_s'],
                        'reserved_usd': reservation}
                    reservations.append(dict(row)); write_json(args.output, state)
                    work.append((row, clip, pcm))
                tasks = [asyncio.create_task(run_reserved(*item)) for item in work]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if service:
            service.close()
        state['summary'] = summarise(rows, selected, args.repeats, len(clips))
        state['missing_credentials'] = {p: PROVIDERS[p].key or 'AWS profile' for p in selected if p not in available}
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        write_json(args.output, state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--providers', default='all')
    parser.add_argument('--profile', default=None)
    parser.add_argument('--region', default='ap-south-1')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--max-estimated-usd', type=float, default=1)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    asyncio.run(main(parser.parse_args()))
