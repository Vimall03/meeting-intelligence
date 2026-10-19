"""Reproducible, real-time comparison of the shipped capture path and streaming.

The manifest contains PCM WAVs and timestamped ground-truth utterances. Audio is
replayed at wall-clock speed to BOTH providers concurrently. Results never enter
the production meeting service. Credentials are read through normal settings.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import re
import subprocess
import time
import unicodedata
import wave
from pathlib import Path

import httpx

from app.core.config import settings
from app.services.openrouter_stt_service import OpenRouterTranscriptionService
from app.services.streaming_stt import AmazonStreamingTranscription


def number_words(n):
    small = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
    tens = 'zero ten twenty thirty forty fifty sixty seventy eighty ninety'.split()
    if n < 20: return small[n]
    if n < 100: return tens[n // 10] + (' ' + small[n % 10] if n % 10 else '')
    if n < 1000: return small[n // 100] + ' hundred' + (' ' + number_words(n % 100) if n % 100 else '')
    if n < 1000000: return number_words(n // 1000) + ' thousand' + (' ' + number_words(n % 1000) if n % 1000 else '')
    return str(n)


def tokens(text):
    text = unicodedata.normalize('NFKC', text).casefold().replace('’', "'")
    text = re.sub(r'\$\s*(\d[\d,]*)(?![\d,]|\.\d)',r'\1 dollars',text)
    text = re.sub(r'\b(\d+)(?:st|nd|rd|th)\b',r'\1',text)
    ordinals={'first':'one','second':'two','third':'three','fourth':'four','fifth':'five',
        'sixth':'six','seventh':'seven','eighth':'eight','ninth':'nine','tenth':'ten',
        'eleventh':'eleven','twelfth':'twelve','twentieth':'twenty','thirtieth':'thirty'}
    text = re.sub(r'\b('+ '|'.join(ordinals) + r')\b',lambda m:ordinals[m[0]],text)
    text = re.sub(r'\bnov\b','november',text)
    text = re.sub(r'(?<=\d),(?=\d)', '', text)
    text = re.sub(r'\b\d+\b', lambda m: number_words(int(m[0])), text)
    # Punctuation, case and numeric formatting are not recognition errors.
    return re.findall(r"[\w]+(?:'[\w]+)?", text)


def alignment(reference, hypothesis):
    """Levenshtein WER with explicit substitutions/deletions/insertions + mapping."""
    costs = [[0] * (len(hypothesis) + 1) for _ in range(len(reference) + 1)]
    for i in range(len(reference) + 1): costs[i][0] = i
    for j in range(len(hypothesis) + 1): costs[0][j] = j
    for i, ref in enumerate(reference, 1):
        for j, hyp in enumerate(hypothesis, 1):
            costs[i][j] = min(costs[i-1][j] + 1, costs[i][j-1] + 1,
                costs[i-1][j-1] + (ref != hyp))
    i, j = len(reference), len(hypothesis)
    substitutions = deletions = insertions = 0
    matches = {}
    while i or j:
        if i and j and costs[i][j] == costs[i-1][j-1] + (reference[i-1] != hypothesis[j-1]):
            if reference[i-1] == hypothesis[j-1]: matches[i-1] = j-1
            else: substitutions += 1
            i -= 1; j -= 1
        elif i and costs[i][j] == costs[i-1][j] + 1:
            deletions += 1; i -= 1
        else:
            insertions += 1; j -= 1
    return {'substitutions': substitutions, 'deletions': deletions, 'insertions': insertions,
        'reference_words': len(reference), 'wer': costs[-1][-1] / max(1, len(reference))}, matches


def percentile(values, p):
    if not values: return None
    values = sorted(values)
    index = (len(values) - 1) * p
    lo = math.floor(index); hi = math.ceil(index)
    return round(values[lo] + (values[hi] - values[lo]) * (index - lo), 4)


def score(clip, events):
    final = [e for e in events if e['final']]
    hypothesis = [word for e in final for word in tokens(e['text'])]
    availability = [e['received_s'] for e in final for _ in tokens(e['text'])]
    reference = [word for u in clip['utterances'] for word in tokens(u['text'])]
    metrics, matches = alignment(reference, hypothesis)
    lags = []; indexed_lags = []; index = 0
    for utterance_index, utterance in enumerate(clip['utterances']):
        index += len(tokens(utterance['text']))
        if index - 1 in matches:
            lag = availability[matches[index-1]] - utterance['end']
            lags.append(lag)
            indexed_lags.append({'utterance_index':utterance_index,'lag_s':lag})
    metrics.update(final_utterance_lags_s=lags, latency_coverage=len(lags),
        utterance_count=len(clip['utterances']), indexed_final_lags=indexed_lags,
        final_text=' '.join(e['text'] for e in final),
        # Initial partial is a UI-only metric; it is not a final/correct answer.
        first_partial_after_speech_s=next((e['received_s']-clip['utterances'][0]['start']
            for e in events if not e['final']), None))
    return metrics


async def compare(clip, folder, *, region, profile, streaming_rate):
    audio_path=(folder / clip['audio']).resolve()
    if not audio_path.is_relative_to(folder.resolve()): raise ValueError('Audio must be inside the corpus folder')
    if hashlib.sha256(audio_path.read_bytes()).hexdigest()!=clip['sha256']: raise ValueError('Corpus audio hash mismatch')
    with wave.open(str(audio_path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        pcm = wav.readframes(wav.getnframes())
    duration = len(pcm) / 32000
    if not 0 < duration <= 120: raise ValueError('Benchmark clips must be at most two minutes')
    # Independently encode each window, matching the shipped MediaRecorder path.
    windows = []
    for offset in range(0, len(pcm), 96000):
        audio = pcm[offset:offset+96000]
        target = folder / f"window-{offset}.webm"
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 's16le',
            '-ar', '16000', '-ac', '1', '-i', 'pipe:0', '-c:a', 'libopus', str(target)],
            input=audio, capture_output=True, check=True)
        windows.append((min(duration, (offset+len(audio))/32000), target))
    service = OpenRouterTranscriptionService(settings.openrouter_api_key,
        model=settings.openrouter_stt_model, endpoint=settings.openrouter_stt_url)
    stream = AmazonStreamingTranscription(region=region, profile=profile)
    setup = time.perf_counter()
    await stream.start()
    setup_ms = (time.perf_counter()-setup)*1000
    began = time.perf_counter()
    batch_events, stream_events, usage = [], [], []
    failures = {'batch': [], 'streaming': []}

    async def receive():
        try:
            async for update in stream.updates():
                stream_events.append({**update.__dict__, 'received_s': time.perf_counter()-began})
        except Exception as exc:
            failures['streaming'].append({'type':type(exc).__name__})

    async def send():
        # Each frame is sent after its samples would have been captured.
        for offset in range(0, len(pcm), 6400):
            audio = pcm[offset:offset+6400]
            deadline = began+(offset+len(audio))/32000
            await asyncio.sleep(max(0, deadline-time.perf_counter()))
            await stream.send(audio)
        await stream.finish()

    async def batch():
        for end, path in windows:
            await asyncio.sleep(max(0, began+end-time.perf_counter()))
            try:
                result = await asyncio.to_thread(service.transcribe, str(path))
            except httpx.HTTPError as exc:
                failures['batch'].append({'type':type(exc).__name__,
                    'status':exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None,
                    'audio_end_s':end})
                usage.append(None)
                continue
            text = ' '.join(s['text'] for s in result['segments'])
            batch_events.append({'text': text, 'final': True,
                'received_s': time.perf_counter()-began, 'audio_end_s': end})
            usage.append(result.get('usage_cost_usd'))
    tasks = [asyncio.create_task(coro()) for coro in (send, receive, batch)]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=duration+90)
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await stream.finish()
        service.close()
        for _, path in windows: path.unlink(missing_ok=True)
    actual_usage = all(type(x) in (int, float) for x in usage)
    return {'clip': clip['id'], 'category': clip['category'], 'duration_s': duration,
        'batch': {**score(clip, batch_events), 'events': batch_events,
            'requests': len(windows), 'failures':failures['batch'],
            'cost_usd': sum(usage) if actual_usage else duration*.000075,
            'cost_basis': 'provider_reported' if actual_usage else 'published_rate_estimate'},
        'streaming': {**score(clip, stream_events), 'events': stream_events,
            'setup_ms': setup_ms, 'requests': 1, 'failures':failures['streaming'],
            'cost_usd': max(15, math.ceil(duration)) / 60 * streaming_rate,
            'cost_basis': 'regional_list_price_estimate_15_second_minimum'}}


def summarise(rows):
    summary = {}
    for provider in ('batch', 'streaming'):
        data = [r[provider] for r in rows]
        errors = sum(d[k] for d in data for k in ('substitutions','deletions','insertions'))
        words = sum(d['reference_words'] for d in data)
        lags = [lag for d in data for lag in d['final_utterance_lags_s']]
        summary[provider] = {'wer': errors/max(1, words), 'reference_words': words,
            'errors': errors, 'final_latency_p50_s': percentile(lags,.5),
            'final_latency_p95_s': percentile(lags,.95),
            'latency_coverage': len(lags), 'utterance_count':sum(d['utterance_count'] for d in data),
            'cost_usd': sum(d['cost_usd'] for d in data),
            'failed_requests':sum(len(d.get('failures',[])) for d in data),
            'requests': sum(d['requests'] for d in data)}
    return summary


def comparison_details(rows):
    paired={'batch':[],'streaming':[]}
    for row in rows:
        indexes={p:{v['utterance_index']:v['lag_s'] for v in row[p]['indexed_final_lags']} for p in paired}
        common=indexes['batch'].keys()&indexes['streaming'].keys()
        for p in paired:paired[p].extend(indexes[p][i] for i in sorted(common))
    partials=[row['streaming']['first_partial_after_speech_s'] for row in rows
        if row['streaming']['first_partial_after_speech_s'] is not None]
    return {'paired_final_latency':{p:{'samples':len(v),'p50_s':percentile(v,.5),'p95_s':percentile(v,.95)} for p,v in paired.items()},
        'streaming_first_partial_s':{'p50':percentile(partials,.5)},
        'streaming_setup_ms':{'p50':percentile([row['streaming']['setup_ms'] for row in rows],.5),
            'p95':percentile([row['streaming']['setup_ms'] for row in rows],.95)}}


async def whole_clip_control(manifest, folder):
    rows=[]
    async with httpx.AsyncClient(timeout=60) as client:
        for clip in manifest['clips']:
            path=(folder/clip['audio']).resolve()
            if not path.is_relative_to(folder.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=clip['sha256']:
                raise ValueError('Corpus audio hash or location mismatch')
            with wave.open(str(path)) as audio:
                if audio.getnframes()/audio.getframerate()>120:raise ValueError('Clip too long')
            response=await client.post(settings.openrouter_stt_url,
                headers={'Authorization':'Bearer '+settings.openrouter_api_key},
                json={'model':settings.openrouter_stt_model,'input_audio':{
                    'data':base64.b64encode(path.read_bytes()).decode(),'format':'wav'},'response_format':'json'})
            response.raise_for_status()
            result=response.json();text=result.get('text','')
            stats,_=alignment(tokens(' '.join(u['text'] for u in clip['utterances'])),tokens(text))
            rows.append({'clip':clip['id'],'category':clip['category'],**stats,
                'text':text,'cost_usd':result.get('usage',{}).get('cost')})
    return {'purpose':'Offline whole-clip accuracy control; not a live latency comparison','rows':rows,
        'aggregate_wer':sum(row[k] for row in rows for k in ('substitutions','deletions','insertions'))/sum(row['reference_words'] for row in rows)}


async def main(args):
    manifest = json.loads(args.manifest.read_text())
    if not 1 <= args.repeats <= 5 or not 1 <= len(manifest['clips']) <= 100:
        raise ValueError('Use 1–5 repeats and 1–100 clips')
    if sum(c['duration_s'] for c in manifest['clips'])*args.repeats > 3600:
        raise ValueError('This benchmark is limited to one replayed hour per provider')
    if not settings.openrouter_api_key: raise SystemExit('Configure OPENROUTER_API_KEY before running.')
    fingerprint=hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    rows=[]
    if args.resume and args.output.exists():
        previous=json.loads(args.output.read_text())
        if previous.get('manifest_sha256',fingerprint)!=fingerprint or previous['region']!=args.region or previous['batch_model']!=settings.openrouter_stt_model or previous['streaming_rate_usd_per_minute']!=args.streaming_rate:
            raise ValueError('Resume requires the same corpus, models, region and prices')
        rows=previous['rows']
        clips_by_id={c['id']:c for c in manifest['clips']}
        for row in rows:
            for provider in ('batch','streaming'):
                row[provider].update(score(clips_by_id[row['clip']],row[provider]['events']))
    for repeat in range(args.repeats):
        # Reverse order on alternating passes to reduce order/time-of-run effects.
        clips = manifest['clips'] if repeat % 2 == 0 else list(reversed(manifest['clips']))
        for clip in clips:
            if any(r['clip']==clip['id'] and r['repeat']==repeat+1 for r in rows): continue
            print(f"Run {repeat+1}/{args.repeats}: {clip['id']}", flush=True)
            row = await compare(clip, args.manifest.parent, region=args.region,
                profile=args.profile, streaming_rate=args.streaming_rate)
            row['repeat'] = repeat + 1
            rows.append(row)
            output = {'schema':1, 'manifest_sha256':fingerprint, 'region':args.region, 'streaming_rate_usd_per_minute':args.streaming_rate,
                'batch_model':settings.openrouter_stt_model, 'streaming_model':'amazon-transcribe-standard',
                'corpus':manifest.get('provenance'), 'rows':rows, 'summary':summarise(rows),
                **comparison_details(rows),
                'by_category':{c:summarise([r for r in rows if r['category']==c]) for c in sorted({r['category'] for r in rows})}}
            args.output.write_text(json.dumps(output,indent=2)+'\n')
            print(json.dumps({'clip':clip['id'], 'batch_wer':row['batch']['wer'],
                'stream_wer':row['streaming']['wer'], 'summary':output['summary']}),flush=True)
    if args.whole_clip_control:
        result=await whole_clip_control(manifest,args.manifest.parent)
        args.whole_clip_control.write_text(json.dumps(result,indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--profile',default=None)
    parser.add_argument('--region',default='ap-south-1')
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--resume',action='store_true',help='Resume the same manifest/settings after an interrupted run')
    parser.add_argument('--whole-clip-control',type=Path,help='Also run an offline whole-clip accuracy control and save it here')
    parser.add_argument('--streaming-rate',type=float,required=True,help='Verified regional USD/minute list price')
    asyncio.run(main(parser.parse_args()))
