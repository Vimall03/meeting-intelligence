# OpenRouter transcription comparison — 20 October 2026

**Status: all seven OpenRouter configurations measured.**

**The requested peer-provider streaming comparison remains incomplete. This is a supplemental chunked-model study; AWS is the only service measured with continuous audio input.**

**Recommendation from this small test: evaluate MAI-Transcribe 2 on broader held-out meeting audio next.** It had 47% fewer word errors than the fresh GPT chunks and 63% lower model charges, with similar typical delay. AWS streaming remains more accurate and typically faster on this corpus. This supports a cheaper candidate for the current chunk path; it does not establish equal streaming performance.

The [21 October controlled AWS streaming test](../stt-streaming-control-2026-10-21/REPORT.md) compares continuous recognition with context resets using the same AWS service and PCM audio.

All seven candidates use the existing OpenRouter key, sent only to OpenRouter. **These are completed three-second audio requests, not persistent streaming.** The AWS row is the earlier continuous-streaming test on identical source audio. No production provider, package, deployment or meeting behaviour changed.

## Accuracy, latency and cost

WER is word error rate; lower is better. Final latency is delay from the end of a reference utterance until its correctly aligned final word becomes available. p95 shows slow responses. Each complete row scores 810 word occurrences across 30 replays of 10 clips (270 distinct words).

| Tested configuration | Runs | WER ↓ | Final median / p95 ↓ | Matched endings | List USD/hour | Test USD |
|---|---:|---:|---:|---:|---:|---:|
| OpenAI: GPT Transcribe via OpenRouter, chunks | 30/30 | 11.11% | 2.06 s / 3.59 s | 63/66 | $0.270 | 0.02972 |
| Deepgram: Nova-3 via OpenRouter, chunks | 30/30 | 11.85% | 1.99 s / 3.18 s | 57/66 | $0.258 | 0.02707 |
| AssemblyAI: Universal-3.6 Pro via OpenRouter, chunks | 30/30 | 5.56% | 1.82 s / 3.10 s | 63/66 | $0.300 | 0.03147 |
| Mistral: Voxtral Mini Transcribe via OpenRouter, chunks | 30/30 | 10.00% | 2.01 s / 3.22 s | 63/66 | $0.180 | 0.01875 |
| Microsoft AI: MAI-Transcribe 2 via OpenRouter, chunks | 30/30 | 5.93% | 1.97 s / 3.07 s | 63/66 | $0.100 | 0.01108 |
| NVIDIA: Parakeet TDT 0.6B v3 via OpenRouter, chunks | 30/30 | 26.67% | 2.06 s / 3.42 s | 54/66 | $0.090 | 0.00944 |
| OpenAI: Whisper Large V3 Turbo via OpenRouter, chunks | 30/30 | 7.41% | 3.05 s / 18.21 s | 63/66 | $0.012 | 0.00217 |
| AWS Transcribe Mumbai, continuous streaming (earlier run) | 30/30 | 2.96% | 0.88 s / 6.64 s | 66/66 | $0.360 | $0.03990 |

Incorrect/missing final words count in WER and are excluded from latency; the coverage column makes those exclusions visible. A fast row with poor accuracy or low coverage is not automatically better. List prices exclude taxes, funding fees, credits, infrastructure and downstream answers/recaps.

## Reported charges and effective cost on this workload

| OpenRouter configuration | Provider-reported USD | Estimated unreported USD | Observed USD/audio hour | Failed windows / sessions |
|---|---:|---:|---:|---:|
| OpenAI: GPT Transcribe | $0.02970 | $0.00002 | 0.283 | 3 / 0 |
| Deepgram: Nova-3 | $0.02707 | $0.00000 | 0.258 | 0 / 0 |
| AssemblyAI: Universal-3.6 Pro | $0.03147 | $0.00000 | 0.300 | 0 / 0 |
| Mistral: Voxtral Mini Transcribe | $0.01875 | $0.00000 | 0.179 | 0 / 0 |
| Microsoft AI: MAI-Transcribe 2 | $0.01108 | $0.00000 | 0.106 | 0 / 0 |
| NVIDIA: Parakeet TDT 0.6B v3 | $0.00944 | $0.00000 | 0.090 | 0 / 0 |
| OpenAI: Whisper Large V3 Turbo | $0.00217 | $0.00000 | 0.021 | 0 / 0 |

New OpenRouter model charges reported by the API: **$0.12969**. Additional estimate for unreported/rejected windows: **$0.00002**. Failed-session reservations still needing reconciliation: **$0.00000**.

Reported values come from `usage.cost`; they are not a finalized account invoice. Estimated failed windows are shown separately rather than pretending their bill is known. The observed hourly equivalent includes request-size/rounding effects on this short-clip workload. It is not a guaranteed forecast for long meetings. The AWS test charge is a regional list-price estimate using one-second rounding and the current published no-minimum rule.

### Slowest final result

| OpenRouter configuration | Maximum final-word delay |
|---|---:|
| OpenAI: GPT Transcribe | 4.61 s |
| Deepgram: Nova-3 | 3.77 s |
| AssemblyAI: Universal-3.6 Pro | 4.39 s |
| Mistral: Voxtral Mini Transcribe | 3.72 s |
| Microsoft AI: MAI-Transcribe 2 | 3.25 s |
| NVIDIA: Parakeet TDT 0.6B v3 | 3.59 s |
| OpenAI: Whisper Large V3 Turbo | 46.09 s |

## Comparison on identical matched utterances

| Candidate | Common endings | Candidate median / p95 | Fresh GPT control median / p95 |
|---|---:|---:|---:|
| Deepgram: Nova-3 | 57 | 1.99 s / 3.18 s | 2.26 s / 3.79 s |
| AssemblyAI: Universal-3.6 Pro | 60 | 1.84 s / 3.11 s | 2.04 s / 3.45 s |
| Mistral: Voxtral Mini Transcribe | 63 | 2.01 s / 3.22 s | 2.06 s / 3.59 s |
| Microsoft AI: MAI-Transcribe 2 | 63 | 1.97 s / 3.07 s | 2.06 s / 3.59 s |
| NVIDIA: Parakeet TDT 0.6B v3 | 54 | 2.06 s / 3.42 s | 2.03 s / 3.92 s |
| OpenAI: Whisper Large V3 Turbo | 63 | 3.05 s / 18.21 s | 2.06 s / 3.59 s |

## Clean, noisy and human-read audio

| Configuration | Category | WER | Final median / p95 | Matched endings |
|---|---|---:|---:|---:|
| OpenAI: GPT Transcribe | human_read_speech | 9.46% | 1.97 s / 2.57 s | 9/12 |
| OpenAI: GPT Transcribe | synthetic_clean | 8.38% | 2.06 s / 4.32 s | 45/45 |
| OpenAI: GPT Transcribe | synthetic_noisy | 31.03% | 2.66 s / 3.25 s | 9/9 |
| Deepgram: Nova-3 | human_read_speech | 13.51% | 1.56 s / 1.99 s | 6/12 |
| Deepgram: Nova-3 | synthetic_clean | 8.58% | 1.98 s / 3.39 s | 42/45 |
| Deepgram: Nova-3 | synthetic_noisy | 26.44% | 2.38 s / 3.10 s | 9/9 |
| AssemblyAI: Universal-3.6 Pro | human_read_speech | 2.70% | 1.42 s / 1.95 s | 12/12 |
| AssemblyAI: Universal-3.6 Pro | synthetic_clean | 4.79% | 1.76 s / 3.13 s | 42/45 |
| AssemblyAI: Universal-3.6 Pro | synthetic_noisy | 17.24% | 2.34 s / 3.04 s | 9/9 |
| Mistral: Voxtral Mini Transcribe | human_read_speech | 9.46% | 1.98 s / 2.37 s | 9/12 |
| Mistral: Voxtral Mini Transcribe | synthetic_clean | 8.38% | 1.97 s / 3.19 s | 45/45 |
| Mistral: Voxtral Mini Transcribe | synthetic_noisy | 20.69% | 2.53 s / 3.26 s | 9/9 |
| Microsoft AI: MAI-Transcribe 2 | human_read_speech | 6.76% | 1.97 s / 2.20 s | 9/12 |
| Microsoft AI: MAI-Transcribe 2 | synthetic_clean | 4.79% | 1.93 s / 3.07 s | 45/45 |
| Microsoft AI: MAI-Transcribe 2 | synthetic_noisy | 10.34% | 2.50 s / 3.12 s | 9/9 |
| NVIDIA: Parakeet TDT 0.6B v3 | human_read_speech | 35.14% | 1.57 s / 1.71 s | 3/12 |
| NVIDIA: Parakeet TDT 0.6B v3 | synthetic_clean | 20.96% | 1.89 s / 3.52 s | 42/45 |
| NVIDIA: Parakeet TDT 0.6B v3 | synthetic_noisy | 37.93% | 2.84 s / 3.33 s | 9/9 |
| OpenAI: Whisper Large V3 Turbo | human_read_speech | 5.41% | 2.89 s / 3.67 s | 9/12 |
| OpenAI: Whisper Large V3 Turbo | synthetic_clean | 6.59% | 2.90 s / 6.80 s | 45/45 |
| OpenAI: Whisper Large V3 Turbo | synthetic_noisy | 17.24% | 16.54 s / 45.71 s | 9/9 |

## All earlier tested paths

Historical observations remain separate; no repeats are pooled across experiments.

| Date / path | WER | Final median / p95 | Cost basis |
|---|---:|---:|---|
| 19 October: GPT chunks | 11.11% | 1.97 s / 3.18 s | $0.02972, mixed reported/estimated |
| 19 October: AWS streaming | 2.96% | 0.85 s / 6.65 s | $0.04830, historical conservative estimate |
| 20 October, earlier: GPT chunks | 10.86% | 2.16 s / 3.34 s | $0.02972, mixed reported/estimated |
| 20 October, earlier: AWS streaming | 2.96% | 0.88 s / 6.64 s | $0.04830, historical conservative estimate |
| 19 October: whole-clip GPT offline control | 0.37% | Not a live test | 0.00997, reported |

The offline control gets the complete recording before transcription; its accuracy does not establish live performance. Original AWS raw estimates include a historical 15-second minimum. Current public pricing says no minimum; the main table uses the refreshed $0.03990 estimate for the earlier 20 October run.

## What the existing key does and does not cover

- The current OpenRouter transcription catalog lists Deepgram Nova-3 and AssemblyAI Universal-3.6 Pro. The tested AssemblyAI model is the Sync API model, not the $0.15/hour Universal-Streaming model.
- Soniox and `gpt-live-transcribe` were not present in the fetched transcription catalog. Their direct WebSocket benchmarks remain untested; an OpenRouter key is not a credential for their direct endpoints.
- OpenRouter documents completed-audio transcription requests. No persistent audio-input streaming endpoint was established in this review. A model being streaming-capable elsewhere does not prove that transport exists through OpenRouter.
- All seven tests use normal OpenRouter-hosted routing. No new provider account, direct key, BYOK connection or production configuration was created.

## Method, limits and execution checks

- Same source hashes and normalizer as the original experiment: 10 clips, 270 distinct words, 22 reference utterances and three repeats. Six synthetic meeting scenarios plus four public LibriSpeech human-read clips; no private recordings.
- Seven models replay concurrently with one outstanding request per model, using separate replay clocks. Each receives independently encoded three-second WebM/Opus windows through the unchanged application transcription service, which decodes to WAV before calling OpenRouter. One reusable HTTP client per model.
- Final latency includes capture-window wait, local decode/encode, HTTP/provider time and sequential-request backlog. The fresh GPT control shares this local seven-model workload; earlier AWS and GPT runs used a different local load. Pairwise rows use identical clip/repeat/utterance matches. There is no interim-text metric for this completed-request path.
- The very short final window is retained to match the previous experiments. Rejected windows remain visible, not silently removed. Other request failures stop that model instead of continuing paid calls against an unusable service.
- No tuning against reference text, keyword hints or paid speaker-label add-ons. No real conversational overlap, code-switching, held-out human meetings, live Google Meet/Zoom capture or production-region concurrency test.
- At most three repeats and one audio hour/model; default $1 local estimate guard. A conservative reservation assumes 15 billable seconds per window. This is a local spending estimate, not a provider-enforced invoice cap. No automatic retries; completed or interrupted reservations are not silently repeated.
- Accuracy on this small set is not evidence of equivalent general meeting performance. Any proposed switch needs broader held-out audio, recovery testing and an explicit decision after reviewing these results.

Machine-readable evidence: [results.json](openrouter-results.json), [catalog and prices](openrouter-catalog.json), [earlier 20 October report](../stt-providers-2026-10-20/REPORT.md), [original report and audio attribution](../stt-2026-10-19/REPORT.md).

## Reproduce / resume

From `backend/`:

```sh
.venv/bin/python stt_openrouter_benchmark.py /path/to/corpus/manifest.json \
  --catalog /path/to/openrouter-catalog.json --output /tmp/mi-openrouter-results.json
# Review the plan; append --execute to permit paid calls.
# Resume requires the same code, catalog, corpus, repeat count and estimate limit.
```

## Sources

- [OpenRouter speech-to-text contract](https://openrouter.ai/docs/guides/overview/multimodal/stt)
- [OpenRouter transcription model catalog](https://openrouter.ai/api/v1/models?output_modalities=transcription)
- [AWS Transcribe pricing](https://aws.amazon.com/transcribe/pricing/)
- [OpenAI: GPT Transcribe](https://openrouter.ai/openai/gpt-transcribe)
- [Deepgram: Nova-3](https://openrouter.ai/deepgram/nova-3)
- [AssemblyAI: Universal-3.6 Pro](https://openrouter.ai/assemblyai/universal-3-6-pro)
- [Mistral: Voxtral Mini Transcribe](https://openrouter.ai/mistralai/voxtral-mini-transcribe)
- [Microsoft AI: MAI-Transcribe 2](https://openrouter.ai/microsoft/mai-transcribe-2)
- [NVIDIA: Parakeet TDT 0.6B v3](https://openrouter.ai/nvidia/parakeet-tdt-0.6b-v3)
- [OpenAI: Whisper Large V3 Turbo](https://openrouter.ai/openai/whisper-large-v3-turbo)

Generated 2026-10-20T16:10:50.168808+00:00.
