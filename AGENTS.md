# Product copy and errors

- Write all visible text for meeting participants. Avoid implementation terms such as backend, OAuth, WebM, raw browser exceptions, URLs, and stack traces in normal product messages.
- Every error and recovery notice must include a stable support code. Add approved copy to `extension/lib/user-messages.ts` or `backend/app/core/user_messages.py`. Keep codes stable when wording changes.
- Use `userError` at extension error display boundaries. Never render raw API bodies, provider error descriptions, or exception messages. New API errors must include a machine code, support code, and safe message; sign-in failure pages must show a support code.
- Position Meeting Intelligence as an assistant for online meetings. Clearly state that Google Meet is supported and Zoom browser capture is a preview pending a live-call check. Do not claim Zoom desktop support or advertise other platforms before their capture adapters exist.
- Preserve the microphone/offscreen lifecycle when changing UI copy. Run the extension typecheck, session checks, and production build for extension changes; run integration route checks when changing sign-in pages.
