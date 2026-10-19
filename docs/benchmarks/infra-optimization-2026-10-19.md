# Recurring infrastructure optimization — 19 October 2026

This pass preserves capture, transcription, acknowledgements, reconnects, recap
finalization, retries, retention, and email delivery behavior. The production
image is based on 0.1.12; unreleased streaming code is excluded.

| Path | Before | After |
| --- | --- | --- |
| Unchanged checkpoint | Encrypt and persist again on five-second renewal | Renew fenced ownership while retaining the existing ciphertext |
| Unchanged recap checkpoint | Upload the same content and encrypt its recovery record again | Renew ownership without another content upload or data-key request |
| Empty recovery sweep | Seven indexed queries every minute | One strongly consistent activity read every minute |
| Unfinished recap | Existing sweep and deadlines | Same sweep and deadlines, plus one activity read |
| Log setup | Local minute check plus remote setup every 30 minutes | Local minute check; remote setup only on deployment |
| Unchanged log configuration | Rewrite service settings and reload systemd | Exit before rewriting or reloading |
| Failure notifications | No configured notification route | Existing alarm events route to the owner-approved SNS email subscription |

The activity counter increments in the session-creation transaction and decrements
in the terminal-cleanup transaction. Missing or uninitialized metadata runs the
full sweep. Enable `RecoveryIdleGateEnabled` only after all writers are upgraded
and the guarded initializer verifies the count. **Disable it before reverting any
writer to older code.** Recovery still checks every minute; no deadlines move.

Cost tradeoff: idle recovery requests fall from 10,080 to 1,440 per day. Seven
empty eventually consistent queries consume at least 3.5 read units; the control
read consumes one, reducing idle read capacity about 71%. Busy recovery adds one
small read per minute and two counter updates per completed recap, with transaction
billing overhead. The pass adds no compute capacity, database indexes, encryption
keys, or additional transcription/model requests. Alarm notifications are event-driven and incur
only applicable event/message delivery usage; email requires SNS confirmation.

Verification includes a simulated unchanged hour (one encryption instead of 721),
fenced-owner and revocation failures, reconnect restoration, retry/uncertain-send
and cleanup behavior, idle-query counts, and deployment-role smoke checks. The
synthetic deployed check uses empty content, generates no AI summary, sends no
recap email, and removes its temporary test records and objects afterward.

Observed after activation: the work index used 0 read units for three consecutive
minutes, compared with 3.5 per minute beforehand. Warm recovery runs took
16.65–18.95 ms, compared with about 234 ms before the original client fix.

Silence filtering and new spending caps are outside this pass because they can
change observable app behavior. This optimization is not a hard spending cap.
