# Streaming transcription comparison — 19 October 2026

**Decision: keep the streaming implementation as a disabled canary.** It improves typical final-text latency and accuracy in this controlled test, but sustained noise produces worse tail latency, and transparent reconnect/replay is not yet implemented for the new transport. The public 0.1.12 package and its Web Store submission are unchanged.

## Results

Both paths received the same audio at real-time speed, concurrently, from the development machine. The existing path used `openai/gpt-transcribe` through OpenRouter on independently encoded three-second WebM/Opus windows. The streaming path used Amazon Transcribe Standard in Mumbai (`ap-south-1`), receiving 200 ms mono 16 kHz signed 16-bit PCM frames over one persistent session per clip. Partial stabilization was enabled at medium stability, with `en-US` recognition.

There were **10 distinct clips, 270 distinct reference words, and 22 reference utterances**, repeated three times: 30 paired runs, 810 scored word occurrences, and 377.70 seconds of audio per provider. Repeats measure timing variability; they do not create additional linguistic diversity. Six clips are synthetic meeting scenarios; four are human read speech from LibriSpeech. This is a small controlled comparison, not proof of accuracy across real meetings.

| Metric | Existing three-second path | Streaming candidate |
|---|---:|---:|
| Aggregate word error rate, lower is better | **11.11%** (90 / 810) | **2.96%** (24 / 810) |
| Final utterance latency, median, matched pairs | **1.97 s** | **0.85 s** |
| Final utterance latency, p95, matched pairs | **3.18 s** | **6.65 s** |
| Correctly matched final-word latency observations | 63 / 66 | 66 / 66 |
| Utterances matched by both paths | 63 | 63 |
| First partial after speech begins, median | Not available | 1.25 s |
| Session setup, median / p95 | HTTP client already constructed | 0.88 s / 1.17 s |
| Transcription requests / persistent sessions | 141 HTTP requests | 30 streaming sessions |
| Rejected requests | 3 | 0 |
| Tested audio cost, excluding infrastructure | About **$0.0297** | About **$0.0483** |

The shared-pair median improved by about **57%**. Aggregate WER decreased by about **73% relative**, or 8.15 percentage points. These differences compare complete provider/codec/transport combinations; they are not a causal estimate of streaming alone.

The partial metric is startup-to-first-preview, not time until a reliable complete answer. Final latency is measured from a reference utterance's acoustic end to availability of its aligned final word. Incorrect/missing final words are excluded from latency, included in WER, and explicitly counted in coverage. The shared-pair figures use the same 63 utterances for both paths. Streaming connection setup is reported separately and excluded from utterance timing. A new HTTP client was created per clip, so the first batch request includes connection establishment; production reuses a shared client. Excluding each clip’s first utterance leaves 36 common observations: median 1.88 s batch versus 0.86 s streaming, and p95 3.26 s versus 6.58 s. Thus the typical-speed and noisy-tail tradeoff persists, but this experiment does not fully reproduce production connection pooling. Cloud deployment, browser meeting capture, downstream search and answer generation are outside this timing measurement.

## Accuracy and noise

| Category | Existing WER | Streaming WER | Existing median / p95 final delay | Streaming median / p95 final delay |
|---|---:|---:|---:|---:|
| Clean synthetic meeting scripts | 8.38% | 1.80% | 1.92 / 3.14 s | 0.85 / 1.02 s |
| Synthetic technical speech with 12 dB stationary noise | 31.03% | 17.24% | 2.48 / 3.15 s | **6.60 / 12.04 s** |
| Human read speech, four short clips | 9.46% | 0.00% | 1.85 / 2.37 s | 0.73 / 0.86 s |

Category latency statistics use each provider's available matches; the main table additionally provides the stricter common-pair comparison. Zero errors on four short read-speech clips is not a general accuracy claim.

The synthetic scenarios cover US, UK, Indian and Australian voices, alternating speakers without overlap, technical vocabulary, budgets, dates, approvals and corrections. The noisy case uses the same UK script with seeded stationary noise. There is no overlapping human conversation, code-switching, actual client call, Zoom test, packet-loss benchmark or production concurrency measurement in this corpus.

An **offline whole-clip control with the existing OpenRouter model scored 0.37% WER** (1 / 270 words). It receives the full WAV clip at once and cannot provide live meeting output. This suggests that short windows and restricted context deserve attention alongside provider selection. The control also changes codec/context, so it does not isolate those effects. Changing the normalizer to handle `21`, `21st`, and `twenty first` equivalently, and `$14,000` as `fourteen thousand dollars` corrected preliminary formatting penalties; all saved results were rescored using the same final rules. No domain-term substitutions were used to improve scores.

The existing path's three rejected requests were repeat occurrences of the final **90 ms** fragment in the alternating-voice clip. Failed fragments are recorded rather than silently omitted. WER uses the actual resulting transcript. An initial exploratory run stopped at this failure; its completed paired measurements were retained, and the runner was changed to record failures and continue. That aborted attempt is excluded from the reported run cost.

## Cost and infrastructure

| Workload | Existing model | Amazon streaming, Mumbai |
|---|---:|---:|
| Published base rate | $0.000075 / audio second | $0.000100 / audio second |
| Continuous meeting hour, list-price arithmetic | **$0.27** | **$0.36** |
| 30 meeting hours | $8.10 | $10.80 |
| 1,000 meeting hours | $270 | $360 |

The AWS rate was retrieved from the **AWS Price List API**, service code `transcribe`, operation `TranscribeAudio`, location `Asia Pacific (Mumbai)`. See [pricing.json](pricing.json) for the exact public SKU and rate dimension. It is a list-price estimate, not a finalized invoice. Minimum billing is 15 seconds per streaming request; all of the short human clips therefore incur disproportionate minimum charges. Continuous long meetings avoid most of that small-clip penalty.

OpenRouter cost uses provider-reported `usage.cost` where available. The three runs containing a rejected tail use duration-based estimates, since rejected requests do not expose comparable usage. Thus the aggregate is a mixed reported/estimated figure, not a fully reconciled bill. Taxes, credits, provider funding fees, EC2, network, DynamoDB/S3/KMS, downstream intent/search/model calls, whole-clip controls and exploratory smoke tests are excluded. Fewer requests do not imply lower provider charges.

The streaming browser transport sends **256 kbit/s**, approximately 1.92 MB/minute of PCM before protocol overhead. That increases browser upload bandwidth relative to compressed capture. It removes the per-window server FFmpeg decode and WAV/base64 conversion from the streaming path; it does not remove transcription inference. No local GPU/model server is added. Actual CPU, memory and production concurrency capacity have not been established by this experiment.

## Implementation and verification

- Version 0.1.13 canary on branch `streaming-transcription`; default builds retain the existing capture method.
- The existing tab/microphone mix feeds a bundled AudioWorklet, producing continuous 200 ms PCM frames. The worklet emits silence to the playback output, avoiding microphone feedback, and flushes the final partial frame.
- Authentication precedes opening the provider connection. AWS credentials stay on the server. The canary requires both `WXT_STREAMING_TRANSCRIPTION=true` at extension build time and `STREAMING_STT_ENABLED=true` on the service.
- Only final results enter meeting context, live search/task processing and recaps. Interim text is replaceable and is not persisted into those paths.
- Stop releases hardware before waiting for the final frame and provider drain. Graceful completion waits for final results. Abrupt interruption is reported with a stable support code and marks enabled recaps incomplete.
- **No transparent reconnect in the canary.** Frame receipt acknowledges input delivery, not a durable finalized transcript. Resume through the legacy protocol is explicitly rejected rather than falsely claiming recovery. The default transport retains its previous replay behaviour.
- A real Brave check passed using the production worklet and PCM capture class, the authenticated local meeting route, and the actual AWS service: 71 frames, 452,608 bytes, 18 partial updates, final text received, last frame acknowledged, audio context closed. Input was synthetic; no microphone, private recording or live meeting was accessed.
- Automated checks cover clipping/frame boundaries, final-frame flush, immediate microphone release, connection-loss failure, disabled mode, authentication, input limits, partial/final separation, live task/search output, recap finalization and resume rejection. See the corresponding backend and extension tests.
- Validation passed: **174 backend checks**, **99 extension checks**, extension typecheck, default and canary production builds, canary ZIP integrity/identity, Linux production-image build, 10 Linux transcription/scorer checks and 6 Linux streaming-route checks with networking disabled. A source secret scan found no leaks. No production deployment, IAM change, website-package replacement or Web Store submission was made for this canary.

## Reproduce

Run from `backend/`, with the normal OpenRouter setting and an AWS profile permitted to call `transcribe:StartStreamTranscription`. The profile used to run the experiment is not embedded in source or the extension. The corpus generator requires macOS `say`, FFmpeg and network access for public samples.

```sh
uv sync --frozen
uv run python make_stt_corpus.py /tmp/mi-stt-corpus
uv run python stt_compare.py /tmp/mi-stt-corpus/manifest.json \
  --profile YOUR_TEST_PROFILE --region ap-south-1 \
  --streaming-rate 0.006 --repeats 3 \
  --output /tmp/mi-stt-results.json \
  --whole-clip-control /tmp/mi-stt-whole-clip-control.json
```

Recheck prices before rerunning. Voice versions can differ across macOS versions; [manifest.json](manifest.json) records the exact original WAV hashes and reference timing. Audio is intentionally excluded from Git. [results.json](results.json) contains the per-run outputs, event times, error counts, common-pair latency statistics and failures. [whole-clip-control.json](whole-clip-control.json) contains the offline control outputs.

LibriSpeech: Vassil Panayotov, Guoguo Chen, Daniel Povey and Sanjeev Khudanpur, **LibriSpeech: an ASR corpus based on public domain audio books** (2015), [OpenSLR resource 12](https://www.openslr.org/12/), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Clips were converted to mono PCM and appended with silence; their reference text was normalized only for scoring. Synthetic meeting scripts contain no private meeting material.

## Promotion gate

Before enabling streaming for public users: implement and fault-test durable replay/recovery, address sustained-noise finalization, benchmark a broader human meeting corpus including overlap and accents, verify real Meet/Zoom audio capture, and measure representative concurrent load from the deployment region. Compare additional streaming providers or models on a held-out corpus rather than tuning this small test set. The present evidence supports continued streaming work, **not an unconditional production switch**.

Sources: [Amazon streaming setup](https://docs.aws.amazon.com/transcribe/latest/dg/streaming-setting-up.html), [Amazon Transcribe pricing](https://aws.amazon.com/transcribe/pricing/), [OpenRouter GPT Transcribe pricing](https://openrouter.ai/openai/gpt-transcribe), [LibriSpeech provenance](https://www.openslr.org/12/).
