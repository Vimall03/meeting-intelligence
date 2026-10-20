# Controlled AWS streaming test — 21 October 2026

**Scope: same AWS service and recognition settings; continuous audio context versus three-second recognition-context resets.**

**The comparison between different providers using genuine streaming remains incomplete. This report measures AWS session boundaries only.**

This is a streaming experiment using existing AWS access. It is not another comparison of completed audio uploads. Both arms receive the same mono 16 kHz PCM samples in 200 ms frames, targeting the same real-time source clock. No OpenRouter transcription calls, new provider accounts, production deployment or infrastructure resources are used.

Recorded paired replays: **30 successful, 0 failed**. A complete run contains 30 pairs from 10 distinct clips repeated three times.

## Accuracy, final latency and cost

| Same AWS model, Mumbai | Word error rate ↓ | Final median / p95 ↓ | Matched final words / utterances | Estimated audio USD |
|---|---:|---:|---:|---:|
| Uninterrupted recognition context | 2.96% | 0.87 s / 6.60 s | 66/66 | $0.03990 |
| Recognition context reset every three seconds | 9.26% | 0.80 s / 1.91 s | 60/66 | $0.03990 |

Latency is measured from the acoustic end of a reference utterance until its correctly aligned final word is available. Incorrect/missing words remain in word error rate and are excluded from latency with coverage shown. Only final text is scored; previews are not treated as stable output.

### Latency on identical matched utterances

| Arm | Common utterances | Median | p95 |
|---|---:|---:|---:|
| Uninterrupted | 60 | 0.87 s | 6.88 s |
| Three-second context resets | 60 | 0.80 s | 1.91 s |

## Results by audio category

| Category | Continuous WER | Reset WER | Continuous median / p95 | Reset median / p95 |
|---|---:|---:|---:|---:|
| human_read_speech | 0.00% | 4.05% | 0.72 s / 0.88 s | 0.55 s / 0.61 s |
| synthetic_clean | 1.80% | 8.38% | 0.87 s / 1.02 s | 0.79 s / 0.99 s |
| synthetic_noisy | 17.24% | 27.59% | 6.57 s / 12.00 s | 1.88 s / 2.47 s |

## Connection setup and input pacing

| Arm | Setup median / p95 | Worst frame-send lateness | Sessions |
|---|---:|---:|---:|
| continuous | 1.06 s / 1.60 s | 0.0355 s | 30 |
| reset | 0.98 s / 1.53 s | 0.5418 s | 141 |

Both initial connections are ready before the shared playback clock starts. Each subsequent reset connection is prepared 1.5 seconds before its audio boundary, so connection establishment does not deliberately delay that arm. Frame lateness above reveals missed scheduling deadlines. Setup is reported separately. Setup that finishes late can still delay audio delivery; that effect remains in the measured utterance delay. The two arms therefore have the same intended pacing, not guaranteed identical arrival timing.

## What this does and does not establish

- Provider, recognition settings, source PCM samples, sample rate and target replay clock are held fixed. The reset arm ends a recognition session at every three-second boundary, losing cross-boundary context and forcing pending results to finalize. The uninterrupted arm retains one context for each entire clip.
- This directly measures the effect of recognition-session boundaries for AWS. It does not isolate context loss from forced finalization, because ending a session does both.
- Measured connection/scheduling delays can perturb audio arrival timing, especially for the reset arm. Those effects are not subtracted or discarded, so this is not a perfect timing-controlled model experiment.
- Neither arm waits for a completed three-second recording before sending its audio. This deliberately removes the capture-buffer wait and codec changes from the context comparison. It is not a replica of the current OpenRouter HTTP path, so its reset-arm latency must not be presented as that path's latency.
- A changed error rate supports an AWS-specific effect of session boundaries; it does not prove the same-sized effect for GPT, MAI or other models. Earlier AWS-versus-GPT tests changed provider/model/codec as well as transport and cannot attribute all differences to streaming.
- The requested comparison between different providers using genuine continuous audio remains incomplete. AWS is the only service measured with continuous input using the available credentials. OpenRouter's documented transcription interface takes completed audio requests; a persistent audio-input endpoint was not established. No OpenRouter key was sent to a direct provider endpoint.
- A no-audio WebSocket upgrade request to OpenRouter's OpenAI-compatible `/api/v1/realtime?intent=transcription` path returned HTTP 404. See the [capability probe](openrouter-capability-probe.json). This establishes that the tested path was unavailable, not that every possible undocumented path was exhaustively checked.
- Six synthetic meeting scenarios plus four public LibriSpeech human-read clips: 270 distinct reference words, 22 distinct reference utterances, 810 scored occurrences per arm at three repeats. No private recordings, overlapping human conversation, code-switching, long human meetings or live Meet/Zoom call.

## Cost and safeguards

- Estimated audio charges for successful pairs: **$0.07980**.
- Conservative pre-call reservations: **$0.25980**. Reservations assume the historical 15-second minimum for every context; the audio estimates above use the current public one-second/no-minimum rule and the Mumbai $0.0001/audio-second rate checked 20 October.
- These are list-price estimates, not reconciled AWS invoices. Taxes, credits/free tier, local compute and application infrastructure are excluded. No storage upload, scheduled worker or infrastructure deployment is part of this experiment.
- Default $1 local estimate guard, maximum three repeats and one replayed audio hour/arm. Failed attempts stop the run; reservations prevent automatic duplicate paid calls after interruption. This is not a provider-enforced invoice cap.

## Evidence and earlier results

- [Raw events, scores, setup and scheduling measurements](results.json)
- [Earlier AWS streaming versus GPT chunks](../stt-providers-2026-10-20/REPORT.md)
- [Supplemental OpenRouter chunked-model comparison](../stt-openrouter-2026-10-20/REPORT.md)
- [Original corpus provenance and offline accuracy control](../stt-2026-10-19/REPORT.md)

## Reproduce

From `backend/`:

```sh
.venv/bin/python stt_aws_context_control.py /path/to/corpus/manifest.json \
  --profile YOUR_EXISTING_PROFILE --output /tmp/mi-aws-context-results.json
# Plan only by default. Append --execute to permit paid calls.
.venv/bin/python stt_aws_context_report.py /tmp/mi-aws-context-results.json \
  --output ../docs/benchmarks/stt-streaming-control-2026-10-21/REPORT.md
```

## Sources

- [AWS streaming formats and real-time pacing recommendations](https://docs.aws.amazon.com/transcribe/latest/dg/streaming.html)
- [AWS Transcribe pricing](https://aws.amazon.com/transcribe/pricing/)
- [OpenRouter speech-to-text request interface](https://openrouter.ai/docs/guides/overview/multimodal/stt)

Generated 2026-10-20T16:10:49.636459+00:00.
