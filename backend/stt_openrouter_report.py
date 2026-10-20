"""One report for OpenRouter candidates and every earlier tested STT path."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from stt_openrouter_benchmark import aggregate
from stt_benchmark_report import cost_for_report, value
from stt_compare import percentile


def pairwise(rows, provider):
    baseline = {(r['repeat'], r['clip']): r for r in rows if r['provider'] == 'gpt_chunks' and r['status'] == 'measured'}
    candidate = []; control = []
    for row in rows:
        if row['provider'] != provider or row['status'] != 'measured':
            continue
        other = baseline.get((row['repeat'], row['clip']))
        if not other:
            continue
        a = {x['utterance_index']: x['lag_s'] for x in row['metrics']['indexed_final_lags']}
        b = {x['utterance_index']: x['lag_s'] for x in other['metrics']['indexed_final_lags']}
        for index in sorted(a.keys() & b.keys()):
            candidate.append(a[index]); control.append(b[index])
    return {name: {'samples': len(values), 'p50_s': percentile(values, .5), 'p95_s': percentile(values, .95)}
        for name, values in [('candidate', candidate), ('gpt_control', control)]}


def render(state, earlier, legacy, offline):
    if state['config']['manifest_sha256'] != earlier['config']['manifest_sha256'] or state['config']['manifest_sha256'] != legacy['manifest_sha256']:
        raise ValueError('MI-BENCH-OR-009: comparison requires identical original audio manifest')
    rows = state['rows']; models = state['config']['models']
    expected = state['config']['clip_count'] * state['config']['repeats']
    summary = {name: aggregate([r for r in rows if r['provider'] == name]) for name in models}
    complete = all(stats['measured_runs'] == expected and not stats['failed_runs'] for stats in summary.values())
    lines = ['# OpenRouter transcription comparison — 20 October 2026', '',
        '**Status: ' + ('all seven OpenRouter configurations measured.' if complete else 'run incomplete; see measured/failed counts.') + '**', '',
        '**The requested peer-provider streaming comparison remains incomplete. This is a supplemental '
        'chunked-model study; AWS is the only service measured with continuous audio input.**', '',
        'The [21 October controlled AWS streaming test](../stt-streaming-control-2026-10-21/REPORT.md) '
        'compares continuous recognition with context resets using the same AWS service and PCM audio.', '',
        'All seven candidates use the existing OpenRouter key, sent only to OpenRouter. '
        '**These are completed three-second audio requests, not persistent streaming.** '
        'The AWS row is the earlier continuous-streaming test on identical source audio. '
        'No production provider, package, deployment or meeting behaviour changed.', '',
        '## Accuracy, latency and cost', '',
        'WER is word error rate; lower is better. Final latency is delay from the end of a reference utterance '
        'until its correctly aligned final word becomes available. p95 shows slow responses. '
        'Each complete row scores 810 word occurrences across 30 replays of 10 clips (270 distinct words).', '',
        '| Tested configuration | Runs | WER ↓ | Final median / p95 ↓ | Matched endings | List USD/hour | Test USD |',
        '|---|---:|---:|---:|---:|---:|---:|']
    if complete and 'mai_chunks' in summary and 'gpt_chunks' in summary:
        mai = summary['mai_chunks']; gpt = summary['gpt_chunks']
        error_reduction = 100 * (1 - mai['wer'] / gpt['wer']) if gpt['wer'] else 0
        cost_reduction = 100 * (1 - mai['test_cost_usd'] / gpt['test_cost_usd']) if gpt['test_cost_usd'] else 0
        finding = ('**Recommendation from this small test: evaluate MAI-Transcribe 2 on broader held-out '
            f'meeting audio next.** It had {error_reduction:.0f}% fewer word errors than the fresh GPT chunks '
            f'and {cost_reduction:.0f}% lower model charges, with similar typical delay. '
            'AWS streaming remains more accurate and typically faster on this corpus. '
            'This supports a cheaper candidate for the current chunk path; it does not establish equal streaming performance.')
        lines[6:6] = [finding, '']
    for name, spec in models.items():
        stats = summary[name]
        lines.append(f"| {spec['name']} via OpenRouter, chunks | {stats['measured_runs']}/{expected} | "
            f"{value(stats['wer'] * 100 if stats['wer'] is not None else None, '%')} | "
            f"{value(stats['final_latency_p50_s'], ' s')} / {value(stats['final_latency_p95_s'], ' s')} | "
            f"{stats['latency_coverage']}/{stats['utterance_count']} | ${spec['hourly_usd']:.3f} | "
            f"{value(stats['test_cost_usd'] if stats['measured_runs'] else None, digits=5)} |")
    aws = earlier['summary']['aws']; aws_cost = cost_for_report('aws', earlier['rows'])
    lines += [f"| AWS Transcribe Mumbai, continuous streaming (earlier run) | {aws['measured_runs']}/30 | "
        f"{aws['wer'] * 100:.2f}% | {aws['final_latency_p50_s']:.2f} s / {aws['final_latency_p95_s']:.2f} s | "
        f"{aws['latency_coverage']}/{aws['utterance_count']} | $0.360 | ${aws_cost:.5f} |", '',
        'Incorrect/missing final words count in WER and are excluded from latency; the coverage column makes '
        'those exclusions visible. A fast row with poor accuracy or low coverage is not automatically better. '
        'List prices exclude taxes, funding fees, credits, infrastructure and downstream answers/recaps.', '',
        '## Reported charges and effective cost on this workload', '',
        '| OpenRouter configuration | Provider-reported USD | Estimated unreported USD | Observed USD/audio hour | Failed windows / sessions |',
        '|---|---:|---:|---:|---:|']
    total_reported = 0; total_estimated = 0
    for name, spec in models.items():
        measured = [r for r in rows if r['provider'] == name and r['status'] == 'measured']
        stats = summary[name]
        reported = sum(r['reported_cost_usd'] for r in measured)
        estimated = sum(r['estimated_unreported_cost_usd'] for r in measured)
        duration = sum(r['duration_s'] for r in measured)
        effective = (reported + estimated) / duration * 3600 if duration else None
        total_reported += reported; total_estimated += estimated
        lines.append(f"| {spec['name']} | ${reported:.5f} | ${estimated:.5f} | {value(effective, digits=3)} | "
            f"{stats['request_failures']} / {stats['failed_runs']} |")
    failed_reserve = sum(r['reserved_usd'] for r in rows if r['status'] == 'failed')
    lines += ['', f'New OpenRouter model charges reported by the API: **${total_reported:.5f}**. '
        f'Additional estimate for unreported/rejected windows: **${total_estimated:.5f}**. '
        f'Failed-session reservations still needing reconciliation: **${failed_reserve:.5f}**.', '',
        'Reported values come from `usage.cost`; they are not a finalized account invoice. '
        'Estimated failed windows are shown separately rather than pretending their bill is known. '
        'The observed hourly equivalent includes request-size/rounding effects on this short-clip workload. '
        'It is not a guaranteed forecast for long meetings. The AWS test charge is a regional list-price '
        'estimate using one-second rounding and the current published no-minimum rule.', '',
        '### Slowest final result', '',
        '| OpenRouter configuration | Maximum final-word delay |',
        '|---|---:|']
    for name, spec in models.items():
        lags = [x['lag_s'] for row in rows if row['provider'] == name and row['status'] == 'measured'
            for x in row['metrics']['indexed_final_lags']]
        lines.append(f"| {spec['name']} | {value(max(lags) if lags else None, ' s')} |")
    lines += ['',
        '## Comparison on identical matched utterances', '',
        '| Candidate | Common endings | Candidate median / p95 | Fresh GPT control median / p95 |',
        '|---|---:|---:|---:|']
    for name, spec in models.items():
        if name == 'gpt_chunks':
            continue
        pair = pairwise(rows, name); a = pair['candidate']; b = pair['gpt_control']
        lines.append(f"| {spec['name']} | {a['samples']} | {value(a['p50_s'], ' s')} / {value(a['p95_s'], ' s')} | "
            f"{value(b['p50_s'], ' s')} / {value(b['p95_s'], ' s')} |")
    lines += ['', '## Clean, noisy and human-read audio', '',
        '| Configuration | Category | WER | Final median / p95 | Matched endings |',
        '|---|---|---:|---:|---:|']
    for name, spec in models.items():
        for category in sorted({r['category'] for r in rows if r['provider'] == name and r['status'] == 'measured'}):
            stats = aggregate([r for r in rows if r['provider'] == name and r['category'] == category])
            lines.append(f"| {spec['name']} | {category} | {stats['wer'] * 100:.2f}% | "
                f"{value(stats['final_latency_p50_s'], ' s')} / {value(stats['final_latency_p95_s'], ' s')} | "
                f"{stats['latency_coverage']}/{stats['utterance_count']} |")
    lines += ['', '## All earlier tested paths', '',
        'Historical observations remain separate; no repeats are pooled across experiments.', '',
        '| Date / path | WER | Final median / p95 | Cost basis |',
        '|---|---:|---:|---|']
    for date, result, mapping in [('19 October', legacy, [('batch', 'GPT chunks'), ('streaming', 'AWS streaming')]),
            ('20 October, earlier', earlier, [('openrouter_chunks', 'GPT chunks'), ('aws', 'AWS streaming')])]:
        for key, name in mapping:
            stats = result['summary'][key]
            if 'paired_final_latency' in result:
                timing = result['paired_final_latency'][key]; p50 = timing['p50_s']; p95 = timing['p95_s']
            else:
                p50 = stats['final_latency_p50_s']; p95 = stats['final_latency_p95_s']
            cost = stats['cost_usd']
            basis = 'mixed reported/estimated' if key in ('batch', 'openrouter_chunks') else 'historical conservative estimate'
            lines.append(f"| {date}: {name} | {stats['wer'] * 100:.2f}% | {p50:.2f} s / {p95:.2f} s | ${cost:.5f}, {basis} |")
    offline_costs = [r.get('cost_usd') for r in offline['rows']]
    offline_cost = sum(offline_costs) if all(type(cost) in (int, float) for cost in offline_costs) else None
    lines += [f"| 19 October: whole-clip GPT offline control | {offline['aggregate_wer'] * 100:.2f}% | Not a live test | {value(offline_cost, digits=5)}, reported |", '',
        'The offline control gets the complete recording before transcription; its accuracy does not establish '
        'live performance. Original AWS raw estimates include a historical 15-second minimum. Current public '
        'pricing says no minimum; the main table uses the refreshed $0.03990 estimate for the earlier 20 October run.', '',
        '## What the existing key does and does not cover', '',
        '- The current OpenRouter transcription catalog lists Deepgram Nova-3 and AssemblyAI Universal-3.6 Pro. '
        'The tested AssemblyAI model is the Sync API model, not the $0.15/hour Universal-Streaming model.',
        '- Soniox and `gpt-live-transcribe` were not present in the fetched transcription catalog. '
        'Their direct WebSocket benchmarks remain untested; an OpenRouter key is not a credential for their direct endpoints.',
        '- OpenRouter documents completed-audio transcription requests. No persistent audio-input streaming endpoint '
        'was established in this review. A model being streaming-capable elsewhere does not prove that transport exists through OpenRouter.',
        '- All seven tests use normal OpenRouter-hosted routing. No new provider account, direct key, BYOK connection '
        'or production configuration was created.', '',
        '## Method, limits and execution checks', '',
        '- Same source hashes and normalizer as the original experiment: 10 clips, 270 distinct words, 22 reference '
        'utterances and three repeats. Six synthetic meeting scenarios plus four public LibriSpeech human-read clips; no private recordings.',
        '- Seven models replay concurrently with one outstanding request per model, using separate replay clocks. '
        'Each receives independently encoded three-second WebM/Opus windows through the unchanged application transcription '
        'service, which decodes to WAV before calling OpenRouter. One reusable HTTP client per model.',
        '- Final latency includes capture-window wait, local decode/encode, HTTP/provider time and sequential-request backlog. '
        'The fresh GPT control shares this local seven-model workload; earlier AWS and GPT runs used a different local load. '
        'Pairwise rows use identical clip/repeat/utterance matches. There is no interim-text metric for this completed-request path.',
        '- The very short final window is retained to match the previous experiments. Rejected windows remain visible, '
        'not silently removed. Other request failures stop that model instead of continuing paid calls against an unusable service.',
        '- No tuning against reference text, keyword hints or paid speaker-label add-ons. No real conversational overlap, '
        'code-switching, held-out human meetings, live Google Meet/Zoom capture or production-region concurrency test.',
        '- At most three repeats and one audio hour/model; default $1 local estimate guard. A conservative reservation '
        'assumes 15 billable seconds per window. This is a local spending estimate, not a provider-enforced invoice cap. '
        'No automatic retries; completed or interrupted reservations are not silently repeated.',
        '- Accuracy on this small set is not evidence of equivalent general meeting performance. Any proposed switch '
        'needs broader held-out audio, recovery testing and an explicit decision after reviewing these results.', '',
        'Machine-readable evidence: [results.json](openrouter-results.json), '
        '[catalog and prices](openrouter-catalog.json), '
        '[earlier 20 October report](../stt-providers-2026-10-20/REPORT.md), '
        '[original report and audio attribution](../stt-2026-10-19/REPORT.md).', '',
        '## Reproduce / resume', '', 'From `backend/`:', '', '```sh',
        '.venv/bin/python stt_openrouter_benchmark.py /path/to/corpus/manifest.json \\',
        '  --catalog /path/to/openrouter-catalog.json --output /tmp/mi-openrouter-results.json',
        '# Review the plan; append --execute to permit paid calls.',
        '# Resume requires the same code, catalog, corpus, repeat count and estimate limit.',
        '```', '', '## Sources', '',
        '- [OpenRouter speech-to-text contract](https://openrouter.ai/docs/guides/overview/multimodal/stt)',
        '- [OpenRouter transcription model catalog](https://openrouter.ai/api/v1/models?output_modalities=transcription)',
        '- [AWS Transcribe pricing](https://aws.amazon.com/transcribe/pricing/)']
    for spec in models.values():
        lines.append(f"- [{spec['name']}]({spec['source']})")
    lines += ['', 'Generated ' + datetime.now(timezone.utc).isoformat() + '.']
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reports = Path(__file__).resolve().parents[1] / 'docs/benchmarks'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(json.loads(args.results.read_text()),
        json.loads((reports / 'stt-providers-2026-10-20/results.json').read_text()),
        json.loads((reports / 'stt-2026-10-19/results.json').read_text()),
        json.loads((reports / 'stt-2026-10-19/whole-clip-control.json').read_text())))
    print(str(args.output))
