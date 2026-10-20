"""AWS streaming context ablation; both arms receive live-paced PCM frames.

The reset arm ends recognition every three seconds. It does NOT simulate an
OpenRouter upload, and no peer-provider streaming performance is implied.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

from app.services.streaming_stt import AmazonStreamingTranscription
from stt_provider_benchmark import read_audio, safe_failure, score_events, write_json
from stt_compare import percentile


WINDOW_BYTES = 96000
FRAME_BYTES = 6400
RATE_PER_SECOND = .0001
PREPARE_AHEAD_S = 1.5


def windows(pcm):
    return [(offset / 32000, pcm[offset:offset + WINDOW_BYTES]) for offset in range(0, len(pcm), WINDOW_BYTES)]


def reservation(duration):
    sizes = [min(3, duration - offset) for offset in range(0, math.ceil(duration), 3)]
    return (max(15, math.ceil(duration)) + sum(max(15, math.ceil(size)) for size in sizes)) * RATE_PER_SECOND


def event(update, *, context, offset, received):
    return {'id': f'{context}:{update.result_id}', 'order': offset + update.start,
        'text': update.text, 'final': update.final, 'received_s': received}


async def paired_replay(pcm, *, region, profile):
    streams = []; tasks = []; setups = []; events = {'continuous': [], 'reset': []}
    timing = {'continuous': [], 'reset': []}

    async def open_stream(arm, context):
        stream = AmazonStreamingTranscription(region=region, profile=profile)
        streams.append(stream); started = time.perf_counter()
        await stream.start()
        setups.append({'arm': arm, 'context': context, 'setup_s': time.perf_counter() - started})
        return stream

    try:
        tasks = [asyncio.create_task(open_stream('continuous', 0)), asyncio.create_task(open_stream('reset', 0))]
        first_continuous, first_reset = await asyncio.gather(*tasks)
        began = time.perf_counter()

        async def replay(stream, audio, *, arm, context, offset):
            late = []
            async def send():
                for position in range(0, len(audio), FRAME_BYTES):
                    frame = audio[position:position + FRAME_BYTES]
                    deadline = began + offset + (position + len(frame)) / 32000
                    await asyncio.sleep(max(0, deadline - time.perf_counter()))
                    late.append(max(0, time.perf_counter() - deadline))
                    await stream.send(frame)
                await stream.finish()

            async def receive():
                async for update in stream.updates():
                    events[arm].append(event(update, context=context, offset=offset,
                        received=time.perf_counter() - began))

            workers = [asyncio.create_task(send()), asyncio.create_task(receive())]
            try:
                async with asyncio.timeout(max(0, offset + len(audio) / 32000 - (time.perf_counter() - began)) + 20):
                    await asyncio.gather(*workers)
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                await stream.finish()
            timing[arm].append({'context': context, 'source_offset_s': offset,
                'audio_s': len(audio) / 32000, 'max_frame_send_lateness_s': max(late, default=0)})

        async def reset_context(context, offset, audio):
            if context:
                await asyncio.sleep(max(0, began + offset - PREPARE_AHEAD_S - time.perf_counter()))
                stream = await open_stream('reset', context)
            else:
                stream = first_reset
            await replay(stream, audio, arm='reset', context=context, offset=offset)

        tasks = [asyncio.create_task(replay(first_continuous, pcm, arm='continuous', context=0, offset=0))]
        tasks += [asyncio.create_task(reset_context(index, offset, audio))
            for index, (offset, audio) in enumerate(windows(pcm))]
        await asyncio.wait_for(asyncio.gather(*tasks), len(pcm) / 32000 + 35)
        return {'events': events, 'setups': setups, 'input_timing': timing}
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(stream.finish() for stream in streams), return_exceptions=True)


def summarise(rows):
    result = {}
    for arm in ('continuous', 'reset'):
        metrics = [row['metrics'][arm] for row in rows if row['status'] == 'measured']
        words = sum(m['reference_words'] for m in metrics)
        errors = sum(m[k] for m in metrics for k in ('substitutions', 'deletions', 'insertions'))
        lags = [v['lag_s'] for m in metrics for v in m['indexed_final_lags']]
        sends = [v['max_frame_send_lateness_s'] for row in rows if row['status'] == 'measured' for v in row['input_timing'][arm]]
        result[arm] = {'wer': errors / words if words else None, 'errors': errors, 'reference_words': words,
            'measured_runs': len(metrics), 'final_latency_p50_s': percentile(lags, .5),
            'final_latency_p95_s': percentile(lags, .95), 'latency_coverage': len(lags),
            'utterance_count': sum(m['utterance_count'] for m in metrics),
            'maximum_frame_send_lateness_s': max(sends, default=None),
            'estimated_cost_usd': sum(row['estimated_cost_usd'][arm] for row in rows if row['status'] == 'measured')}
    paired = {'continuous': [], 'reset': []}
    for row in rows:
        if row['status'] != 'measured':
            continue
        values = {arm: {v['utterance_index']: v['lag_s'] for v in row['metrics'][arm]['indexed_final_lags']} for arm in paired}
        common = values['continuous'].keys() & values['reset'].keys()
        for arm in paired:
            paired[arm].extend(values[arm][index] for index in sorted(common))
    result['common_utterance_latency'] = {arm: {'samples': len(v), 'p50_s': percentile(v, .5), 'p95_s': percentile(v, .95)}
        for arm, v in paired.items()}
    return result


async def main(args):
    manifest = json.loads(args.manifest.read_text()); clips = manifest['clips']
    if not 1 <= args.repeats <= 3 or not 1 <= len(clips) <= 100 or not 0 < args.max_estimated_usd <= 1:
        raise ValueError('MI-BENCH-AWS-001: maximum three repeats, 100 clips and $1 local estimate guard')
    if sum(c['duration_s'] for c in clips) * args.repeats > 3600:
        raise ValueError('MI-BENCH-AWS-005: maximum one audio hour per arm')
    for clip in clips:
        read_audio(args.manifest.resolve().parent, clip)
    bound = sum(reservation(c['duration_s']) for c in clips) * args.repeats
    if bound > args.max_estimated_usd:
        raise ValueError('MI-BENCH-AWS-002: conservative reservation exceeds limit')
    print(json.dumps({'execute': args.execute, 'conservative_reservation_usd': bound,
        'paired_runs': len(clips) * args.repeats, 'continuous_sessions': len(clips) * args.repeats,
        'reset_sessions': sum(math.ceil(c['duration_s'] / 3) for c in clips) * args.repeats,
        'purpose': 'Same AWS model: uninterrupted live stream versus three-second recognition-context resets; not an HTTP chunk replica.'}), flush=True)
    if not args.execute:
        return
    config = {'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'region': args.region, 'repeats': args.repeats, 'rate_usd_per_audio_second': RATE_PER_SECOND,
        'sample_rate': 16000, 'channels': 1, 'encoding': 'pcm16le', 'language': 'en-US',
        'partial_stability': 'medium', 'source_frame_ms': 200, 'context_window_s': 3,
        'prepare_next_context_ahead_s': PREPARE_AHEAD_S, 'max_estimated_usd': args.max_estimated_usd}
    state = {'config': config, 'rows': [], 'reservations': [], 'created_at': datetime.now(timezone.utc).isoformat()}
    if args.output.exists():
        state = json.loads(args.output.read_text())
        if state['config'] != config:
            raise ValueError('MI-BENCH-AWS-003: resume requires identical code and inputs')
    recorded = {(r['repeat'], r['clip']) for r in state['rows']}
    reserved = {(r['repeat'], r['clip']) for r in state['reservations']}
    if reserved - recorded or any(r['status'] == 'failed' for r in state['rows']):
        raise ValueError('MI-BENCH-AWS-004: interrupted/failed paid attempt requires audit; no automatic retry')
    for repeat in range(args.repeats):
        for clip in clips:
            if (repeat, clip['id']) in recorded:
                continue
            item = {'clip': clip['id'], 'repeat': repeat, 'duration_s': clip['duration_s'], 'reserved_usd': reservation(clip['duration_s'])}
            state['reservations'].append(dict(item)); write_json(args.output, state)
            pcm = read_audio(args.manifest.resolve().parent, clip)
            try:
                replay = await paired_replay(pcm, region=args.region, profile=args.profile)
                item.update(replay, status='measured', category=clip['category'],
                    metrics={arm: score_events(clip, replay['events'][arm]) for arm in ('continuous', 'reset')},
                    estimated_cost_usd={'continuous': math.ceil(clip['duration_s']) * RATE_PER_SECOND,
                        'reset': sum(math.ceil(len(audio) / 32000) for _, audio in windows(pcm)) * RATE_PER_SECOND})
            except Exception as exc:
                item.update(status='failed', failure=safe_failure(exc))
            state['rows'].append(item); state['summary'] = summarise(state['rows'])
            state['updated_at'] = datetime.now(timezone.utc).isoformat(); write_json(args.output, state)
            print(json.dumps({k: item[k] for k in ('clip', 'repeat', 'status')}), flush=True)
            if item['status'] == 'failed':
                return


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--region', default='ap-south-1')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-estimated-usd', type=float, default=1)
    parser.add_argument('--execute', action='store_true')
    asyncio.run(main(parser.parse_args()))
