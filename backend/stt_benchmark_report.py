"""Render measured and untested STT options together without inventing results."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from stt_provider_benchmark import PROVIDERS, summarise
from stt_compare import percentile


NAMES = {'openrouter_chunks': 'GPT Transcribe / OpenRouter, 3-second chunks',
    'aws': 'AWS Transcribe, Mumbai', 'soniox': 'Soniox stt-rt-v5',
    'assemblyai': 'AssemblyAI Universal-Streaming English', 'deepgram': 'Deepgram Nova-3 monolingual',
    'openai_turns': 'Direct GPT Transcribe, committed turns', 'openai_live': 'Direct GPT Live Transcribe, low delay'}


def value(number, suffix='', digits=2):
    return '—' if number is None else f'{number:.{digits}f}{suffix}'


def cost_for_report(provider, rows):
    measured = [r for r in rows if r['provider'] == provider and r['status'] == 'measured']
    if not measured:
        return None
    if provider == 'aws':
        # Current pricing page says per-second billing with no minimum. Raw
        # records deliberately preserve the runner's older conservative estimate.
        return sum(math.ceil(r['duration_s']) * .0001 for r in measured)
    return sum(r['cost_usd'] for r in measured)


def render(state, legacy_folder):
    rows = state['rows']; config = state['config']; selected = list(config['providers'])
    manifest_path = legacy_folder / 'manifest.json'
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != config['manifest_sha256']:
        raise ValueError('MI-BENCH-020: report requires the same corpus as the historical comparison')
    clip_count = len(json.loads(manifest_path.read_text())['clips'])
    summary = summarise(rows, selected, config['repeats'], clip_count)
    missing = state.get('missing_credentials', {})
    old = json.loads((legacy_folder / 'results.json').read_text())
    control = json.loads((legacy_folder / 'whole-clip-control.json').read_text())
    all_done = bool(selected) and all(s['status'] == 'complete' for s in summary.values())
    lines = ['# Transcription provider comparison — 20 October 2026', '',
        '**Status: ' + ('all requested providers measured.' if all_done else
        'incomplete; the new streaming providers still require credentials or successful runs.') + '**', '',
        'This report keeps measured accuracy/latency separate from advertised pricing. '
        'A blank measurement means untested, not zero errors or instant results. '
        'No production provider, deployment, extension package or meeting behaviour was changed.', '',
        '## Current comparison', '',
        'Lower word error rate (WER) and lower delay are better. Delay is measured from the acoustic '
        'end of a reference utterance until its correctly aligned final word is available; '
        'it includes buffering, endpoint detection and network/provider processing. '
        'It excludes connection setup, downstream answers and recaps.', '',
        '| Option | Test status | WER | Final delay median / p95 | Base USD/hour | Test USD |',
        '|---|---|---:|---:|---:|---:|']
    for provider, stats in summary.items():
        price = config['providers'][provider]['hourly_usd']
        status = ('Needs API key' if provider in missing and not stats['measured_runs']
            else f"{stats['measured_runs']}/{clip_count * config['repeats']} runs; {stats['status']}")
        wer = value(stats['wer'] * 100 if stats['wer'] is not None else None, '%')
        latency = value(stats['final_latency_p50_s'], ' s') + ' / ' + value(stats['final_latency_p95_s'], ' s')
        cost = cost_for_report(provider, rows)
        lines.append(f"| {NAMES[provider]} | {status} | {wer} | {latency} | ${price:.3f} | {value(cost, digits=5)} |")
    lines += ['', 'Test costs are USD before tax/credits and exclude application infrastructure and downstream models. '
        'OpenRouter uses reported request costs where available, with a duration estimate for a clip containing a rejected tail. '
        'AWS is an estimate from actual replay duration, rounded up to seconds, at the current Mumbai list rate. '
        'Other providers use reported usage/duration where supported or clearly labelled list-rate estimates. '
        '**These are not reconciled invoices.**', '',
        '## Coverage, previews and setup', '',
        '| Option | Final-word matches / utterances | First preview median | Setup median | Request failures |',
        '|---|---:|---:|---:|---:|']
    for provider, stats in summary.items():
        measured = [r for r in rows if r['provider'] == provider and r['status'] == 'measured']
        setups = [r['details']['setup_s'] for r in measured if r['details'].get('setup_s') is not None]
        coverage = f"{stats['latency_coverage']} / {stats['utterance_count']}" if measured else '—'
        lines.append(f"| {NAMES[provider]} | {coverage} | {value(stats['first_partial_p50_s'], ' s')} | "
            f"{value(percentile(setups, .5), ' s')} | {stats['request_failures'] + stats['failed_runs'] if measured or stats['failed_runs'] else '—'} |")
    lines += ['', 'Preview timing is time from the first reference speech onset to the first nonempty interim output. '
        'It is neither a final transcript nor a per-word latency claim. Incorrect/missing final words remain in WER '
        'and are excluded from latency with coverage shown above.', '',
        '### Latency on the same matched utterances', '',
        '| Candidate | Matched pairs | Candidate median / p95 | Chunk baseline median / p95 |',
        '|---|---:|---:|---:|']
    for provider, stats in summary.items():
        pair = stats.get('paired_with_chunks', {})
        if provider not in pair or not pair[provider]['samples']:
            continue
        a = pair[provider]; b = pair['openrouter_chunks']
        lines.append(f"| {NAMES[provider]} | {a['samples']} | {value(a['p50_s'], ' s')} / {value(a['p95_s'], ' s')} | "
            f"{value(b['p50_s'], ' s')} / {value(b['p95_s'], ' s')} |")
    lines += ['', '## Results by audio category', '',
        '| Option | Category | WER | Final delay median / p95 | Coverage |',
        '|---|---|---:|---:|---:|']
    for provider in selected:
        categories = sorted({r['category'] for r in rows if r['provider'] == provider and r['status'] == 'measured'})
        for category in categories:
            measured = [r for r in rows if r['provider'] == provider and r['status'] == 'measured' and r['category'] == category]
            words = sum(r['metrics']['reference_words'] for r in measured)
            errors = sum(r['metrics'][k] for r in measured for k in ('substitutions', 'deletions', 'insertions'))
            lags = [x['lag_s'] for r in measured for x in r['metrics']['indexed_final_lags']]
            utterances = sum(r['metrics']['utterance_count'] for r in measured)
            lines.append(f"| {NAMES[provider]} | {category} | {100 * errors / words:.2f}% | "
                f"{value(percentile(lags, .5), ' s')} / {value(percentile(lags, .95), ' s')} | {len(lags)} / {utterances} |")
    lines += ['', '## Previous tests, preserved separately', '',
        'The 19 October experiment used the identical corpus. These rows are historical observations; '
        'they are not pooled with the new run.', '',
        '| Previously tested option | WER | Matched final delay median / p95 | Historical test USD |',
        '|---|---:|---:|---:|']
    for provider, name in [('batch', 'GPT Transcribe / OpenRouter, 3-second chunks'), ('streaming', 'AWS Transcribe, Mumbai')]:
        stats = old['summary'][provider]; pair = old['paired_final_latency'][provider]
        lines.append(f"| {name} | {stats['wer'] * 100:.2f}% | {pair['p50_s']:.2f} s / {pair['p95_s']:.2f} s | ${stats['cost_usd']:.5f} |")
    control_costs = [r.get('cost_usd') for r in control['rows']]
    control_cost = sum(control_costs) if all(type(c) in (int, float) for c in control_costs) else None
    lines += [f"| GPT Transcribe whole-clip offline control | {control['aggregate_wer'] * 100:.2f}% | Not a live test | {value(control_cost, digits=5)} |", '',
        'The offline control receives the complete clip before transcription. Its accuracy cannot be used '
        'as evidence of live performance. The original AWS short-clip cost used a historical 15-second minimum; '
        'the pricing page now states one-second increments without a minimum. Original raw results remain unchanged.', '',
        '## Price assumptions and scale', '',
        '| Option | 30 continuous hours | 1,000 continuous hours | Price qualifications |',
        '|---|---:|---:|---|']
    for provider in selected:
        spec = config['providers'][provider]; rate = spec['hourly_usd']
        note = spec['note']
        if provider == 'aws':
            note = 'Mumbai public SKU rechecked 20 October; no-minimum billing per current pricing page.'
        lines.append(f"| {NAMES[provider]} | ${rate * 30:.2f} | ${rate * 1000:.2f} | {note or 'Base transcription only.'} |")
    lines += ['', 'AssemblyAI charges the entire connected session; its $0.15/hour model is Universal-Streaming, '
        'not the $0.45/hour Pro Realtime model. Speaker labels add $0.12/hour on AssemblyAI and Deepgram; '
        'Deepgram keyterms add $0.078/hour. No paid speaker-label/keyterm add-ons are enabled in this experiment. '
        'Soniox advertises included speaker labels but its hourly figure is a token-based approximation. '
        'Rates are not interchangeable when adding features, languages or channels.', '',
        '## Method and limits', '',
        '- Same original WAV hashes: 10 distinct clips, 270 distinct reference words and 22 reference utterances; '
        'three repeats produce 810 scored word occurrences per fully tested provider.',
        '- Six synthetic meeting scenarios and four human read-speech clips. No private meeting recordings are used. '
        'No overlapping human conversation, code-switching, live Google Meet/Zoom capture, production-region relay or concurrency/load test.',
        '- Audio replays at wall-clock speed in 200 ms frames. Chunk baseline uses independently encoded three-second '
        'WebM/Opus windows and the existing transcription service, reusing one HTTP client across clips.',
        '- Streaming providers use one session per clip. Soniox endpoint detection is enabled at defaults. '
        'AssemblyAI uses unformatted final turns; Deepgram uses 300 ms endpointing. '
        'OpenAI uses 24 kHz FFmpeg-resampled PCM and a causal energy detector with 400 ms silence; '
        'reference timings/text never drive its commits. Results compare complete configurations, not models in isolation.',
        '- The normalizer is unchanged from the original experiment. Final subwords are concatenated without inserted spaces, '
        'duplicate final segments are excluded, and OpenAI turns are ordered using committed-item metadata.',
        '- This small corpus is a regression/comparison set, not evidence that any model matches the best provider '
        'across real meetings. A production switch requires broader held-out human audio and live-call recovery checks.', '',
        '## Spend and execution audit', '',
        f"- Paid attempts reserved before opening sessions: {len(state['reservations'])}.",
        f"- Sum of conservative per-attempt list-price reservations: ${sum(r['reserved_usd'] for r in state['reservations']):.5f}.",
        '- Default reservation limit is $1; at most three repeats, one replayed hour/provider, and no automatic retries. '
        'This is a local estimate guard, not a provider-enforced invoice cap. Streaming calls have bounded setup/drain time '
        'and close their sockets on completion/failure.',
        '- Every completed attempt saves events, timing, scores and cost basis in [results.json](results.json). '
        'An interrupted reservation is never silently replayed; failed providers stop after the first failed session.']
    completed = {(r['provider'], r['repeat'], r['clip']) for r in rows}
    interrupted = [r for r in state['reservations'] if (r['provider'], r['repeat'], r['clip']) not in completed]
    if interrupted:
        lines.append(f"- **{len(interrupted)} interrupted reservations need manual audit before rerunning.**")
    if missing:
        lines += ['', 'Unfinished live tests require these local environment variables:']
        for key in sorted(set(missing.values())):
            lines.append(f'- `{key}`')
        lines += ['', 'Put keys in the ignored `backend/.env` or the process environment; never put keys in chat or Git. '
            'The other providers cannot be ranked on accuracy or latency until those tests run.']
    lines += ['', '## Reproduce / resume', '', 'From `backend/`:', '', '```sh',
        '.venv/bin/python stt_provider_benchmark.py /path/to/corpus/manifest.json \\',
        '  --profile YOUR_TEST_PROFILE --output /tmp/mi-provider-results.json',
        '# Review the plan; append --execute to permit paid calls.',
        '# Run the identical command again to fill newly configured providers.',
        '.venv/bin/python stt_benchmark_report.py /tmp/mi-provider-results.json \\',
        '  --output ../docs/benchmarks/stt-providers-2026-10-20/REPORT.md', '```', '',
        'Resume requires unchanged code, models, corpus and repeat count. Saved successful/failed/interrupted attempts '
        'are skipped. Audio stays outside Git. The original generator, corpus manifest and licences are documented '
        'in [the original report](../stt-2026-10-19/REPORT.md).', '', '## Sources', '']
    for provider in selected:
        lines.append(f"- [{NAMES[provider]} pricing]({config['providers'][provider]['pricing_url']})")
    lines += ['- [Soniox streaming protocol](https://soniox.com/docs/api-reference/stt/websocket-api)',
        '- [AssemblyAI streaming protocol](https://www.assemblyai.com/docs/streaming/api-spec/streaming-websocket)',
        '- [Deepgram streaming protocol](https://developers.deepgram.com/reference/speech-to-text/listen-streaming)',
        '- [OpenAI realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)', '',
        'Generated ' + datetime.now(timezone.utc).isoformat() + '.']
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--legacy-folder', type=Path,
        default=Path(__file__).resolve().parents[1] / 'docs/benchmarks/stt-2026-10-19')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    state = json.loads(args.results.read_text())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(state, args.legacy_folder))
    print(str(args.output))
