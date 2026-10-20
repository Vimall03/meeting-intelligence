# Transcription provider comparison — 20 October 2026

The [completed OpenRouter comparison](../stt-openrouter-2026-10-20/REPORT.md) now tests seven configurations using the existing key and includes all earlier results. The direct streaming configurations below remain untested.

**Status: incomplete; the new streaming providers still require credentials or successful runs.**

This report keeps measured accuracy/latency separate from advertised pricing. A blank measurement means untested, not zero errors or instant results. No production provider, deployment, extension package or meeting behaviour was changed.

## Current comparison

Lower word error rate (WER) and lower delay are better. Delay is measured from the acoustic end of a reference utterance until its correctly aligned final word is available; it includes buffering, endpoint detection and network/provider processing. It excludes connection setup, downstream answers and recaps.

| Option | Test status | WER | Final delay median / p95 | Base USD/hour | Test USD |
|---|---|---:|---:|---:|---:|
| GPT Transcribe / OpenRouter, 3-second chunks | 30/30 runs; complete | 10.86% | 2.16 s / 3.34 s | $0.270 | 0.02972 |
| AWS Transcribe, Mumbai | 30/30 runs; complete | 2.96% | 0.88 s / 6.64 s | $0.360 | 0.03990 |
| Soniox stt-rt-v5 | Needs API key | — | — / — | $0.120 | — |
| AssemblyAI Universal-Streaming English | Needs API key | — | — / — | $0.150 | — |
| Deepgram Nova-3 monolingual | Needs API key | — | — / — | $0.288 | — |
| Direct GPT Transcribe, committed turns | Needs API key | — | — / — | $0.270 | — |
| Direct GPT Live Transcribe, low delay | Needs API key | — | — / — | $1.020 | — |

Test costs are USD before tax/credits and exclude application infrastructure and downstream models. OpenRouter uses reported request costs where available, with a duration estimate for a clip containing a rejected tail. AWS is an estimate from actual replay duration, rounded up to seconds, at the current Mumbai list rate. Other providers use reported usage/duration where supported or clearly labelled list-rate estimates. **These are not reconciled invoices.**

## Coverage, previews and setup

| Option | Final-word matches / utterances | First preview median | Setup median | Request failures |
|---|---:|---:|---:|---:|
| GPT Transcribe / OpenRouter, 3-second chunks | 63 / 66 | — | — | 3 |
| AWS Transcribe, Mumbai | 66 / 66 | 1.26 s | 0.92 s | 0 |
| Soniox stt-rt-v5 | — | — | — | — |
| AssemblyAI Universal-Streaming English | — | — | — | — |
| Deepgram Nova-3 monolingual | — | — | — | — |
| Direct GPT Transcribe, committed turns | — | — | — | — |
| Direct GPT Live Transcribe, low delay | — | — | — | — |

Preview timing is time from the first reference speech onset to the first nonempty interim output. It is neither a final transcript nor a per-word latency claim. Incorrect/missing final words remain in WER and are excluded from latency with coverage shown above.

### Latency on the same matched utterances

| Candidate | Matched pairs | Candidate median / p95 | Chunk baseline median / p95 |
|---|---:|---:|---:|
| AWS Transcribe, Mumbai | 63 | 0.88 s / 6.64 s | 2.16 s / 3.34 s |

## Results by audio category

| Option | Category | WER | Final delay median / p95 | Coverage |
|---|---|---:|---:|---:|
| GPT Transcribe / OpenRouter, 3-second chunks | human_read_speech | 8.56% | 1.98 s / 2.36 s | 9 / 12 |
| GPT Transcribe / OpenRouter, 3-second chunks | synthetic_clean | 8.38% | 1.99 s / 3.41 s | 45 / 45 |
| GPT Transcribe / OpenRouter, 3-second chunks | synthetic_noisy | 31.03% | 2.59 s / 3.32 s | 9 / 9 |
| AWS Transcribe, Mumbai | human_read_speech | 0.00% | 0.71 s / 0.90 s | 12 / 12 |
| AWS Transcribe, Mumbai | synthetic_clean | 1.80% | 0.88 s / 1.03 s | 45 / 45 |
| AWS Transcribe, Mumbai | synthetic_noisy | 17.24% | 6.63 s / 12.04 s | 9 / 9 |

## Previous tests, preserved separately

The 19 October experiment used the identical corpus. These rows are historical observations; they are not pooled with the new run.

| Previously tested option | WER | Matched final delay median / p95 | Historical test USD |
|---|---:|---:|---:|
| GPT Transcribe / OpenRouter, 3-second chunks | 11.11% | 1.97 s / 3.18 s | $0.02972 |
| AWS Transcribe, Mumbai | 2.96% | 0.85 s / 6.65 s | $0.04830 |
| GPT Transcribe whole-clip offline control | 0.37% | Not a live test | 0.00997 |

The offline control receives the complete clip before transcription. Its accuracy cannot be used as evidence of live performance. The original AWS short-clip cost used a historical 15-second minimum; the pricing page now states one-second increments without a minimum. Original raw results remain unchanged.

## Price assumptions and scale

| Option | 30 continuous hours | 1,000 continuous hours | Price qualifications |
|---|---:|---:|---|
| GPT Transcribe / OpenRouter, 3-second chunks | $8.10 | $270.00 | Base transcription only. |
| AWS Transcribe, Mumbai | $10.80 | $360.00 | Mumbai public SKU rechecked 20 October; no-minimum billing per current pricing page. |
| Soniox stt-rt-v5 | $3.60 | $120.00 | Approximate token-based rate; not an invoice. |
| AssemblyAI Universal-Streaming English | $4.50 | $150.00 | Connected session duration, including silence; no paid add-ons enabled. |
| Deepgram Nova-3 monolingual | $8.64 | $288.00 | Monolingual promotional rate; regular $0.462/hour. No paid add-ons. |
| Direct GPT Transcribe, committed turns | $8.10 | $270.00 | Base transcription only. |
| Direct GPT Live Transcribe, low delay | $30.60 | $1020.00 | Low delay; client energy VAD, not reference utterance boundaries. |

AssemblyAI charges the entire connected session; its $0.15/hour model is Universal-Streaming, not the $0.45/hour Pro Realtime model. Speaker labels add $0.12/hour on AssemblyAI and Deepgram; Deepgram keyterms add $0.078/hour. No paid speaker-label/keyterm add-ons are enabled in this experiment. Soniox advertises included speaker labels but its hourly figure is a token-based approximation. Rates are not interchangeable when adding features, languages or channels.

## Method and limits

- Same original WAV hashes: 10 distinct clips, 270 distinct reference words and 22 reference utterances; three repeats produce 810 scored word occurrences per fully tested provider.
- Six synthetic meeting scenarios and four human read-speech clips. No private meeting recordings are used. No overlapping human conversation, code-switching, live Google Meet/Zoom capture, production-region relay or concurrency/load test.
- Audio replays at wall-clock speed in 200 ms frames. Chunk baseline uses independently encoded three-second WebM/Opus windows and the existing transcription service, reusing one HTTP client across clips.
- Streaming providers use one session per clip. Soniox endpoint detection is enabled at defaults. AssemblyAI uses unformatted final turns; Deepgram uses 300 ms endpointing. OpenAI uses 24 kHz FFmpeg-resampled PCM and a causal energy detector with 400 ms silence; reference timings/text never drive its commits. Results compare complete configurations, not models in isolation.
- The normalizer is unchanged from the original experiment. Final subwords are concatenated without inserted spaces, duplicate final segments are excluded, and OpenAI turns are ordered using committed-item metadata.
- This small corpus is a regression/comparison set, not evidence that any model matches the best provider across real meetings. A production switch requires broader held-out human audio and live-call recovery checks.

## Spend and execution audit

- Paid attempts reserved before opening sessions: 60.
- Sum of conservative per-attempt list-price reservations: $0.24985.
- Default reservation limit is $1; at most three repeats, one replayed hour/provider, and no automatic retries. This is a local estimate guard, not a provider-enforced invoice cap. Streaming calls have bounded setup/drain time and close their sockets on completion/failure.
- Every completed attempt saves events, timing, scores and cost basis in [results.json](results.json). An interrupted reservation is never silently replayed; failed providers stop after the first failed session.

Unfinished live tests require these local environment variables:
- `ASSEMBLYAI_API_KEY`
- `DEEPGRAM_API_KEY`
- `OPENAI_API_KEY`
- `SONIOX_API_KEY`

Put keys in the ignored `backend/.env` or the process environment; never put keys in chat or Git. The other providers cannot be ranked on accuracy or latency until those tests run.

## Reproduce / resume

From `backend/`:

```sh
.venv/bin/python stt_provider_benchmark.py /path/to/corpus/manifest.json \
  --profile YOUR_TEST_PROFILE --output /tmp/mi-provider-results.json
# Review the plan; append --execute to permit paid calls.
# Run the identical command again to fill newly configured providers.
.venv/bin/python stt_benchmark_report.py /tmp/mi-provider-results.json \
  --output ../docs/benchmarks/stt-providers-2026-10-20/REPORT.md
```

Resume requires unchanged code, models, corpus and repeat count. Saved successful/failed/interrupted attempts are skipped. Audio stays outside Git. The original generator, corpus manifest and licences are documented in [the original report](../stt-2026-10-19/REPORT.md).

## Sources

- [GPT Transcribe / OpenRouter, 3-second chunks pricing](https://openrouter.ai/openai/gpt-transcribe)
- [AWS Transcribe, Mumbai pricing](https://aws.amazon.com/transcribe/pricing/)
- [Soniox stt-rt-v5 pricing](https://soniox.com/pricing)
- [AssemblyAI Universal-Streaming English pricing](https://www.assemblyai.com/pricing)
- [Deepgram Nova-3 monolingual pricing](https://deepgram.com/pricing)
- [Direct GPT Transcribe, committed turns pricing](https://developers.openai.com/api/docs/pricing)
- [Direct GPT Live Transcribe, low delay pricing](https://developers.openai.com/api/docs/pricing)
- [Soniox streaming protocol](https://soniox.com/docs/api-reference/stt/websocket-api)
- [AssemblyAI streaming protocol](https://www.assemblyai.com/docs/streaming/api-spec/streaming-websocket)
- [Deepgram streaming protocol](https://developers.deepgram.com/reference/speech-to-text/listen-streaming)
- [OpenAI realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription)

Generated 2026-10-20T14:56:26.311623+00:00.
