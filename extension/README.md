# Meeting Intelligence Chrome extension

Meeting Intelligence captures Google Meet audio with the user's permission and streams it to the local backend. The backend transcribes and analyzes each audio window; the extension displays its search results and task suggestions.

## Local development

```bash
npm install
npm run dev
```

For a production build, run `npm run build`, then load `extension/.output/chrome-mv3` as an unpacked extension in `chrome://extensions`.

The extension detects an entered Meet call by combining a Meet meeting URL with call controls such as “Leave call” or microphone/camera controls. Detection is isolated in `lib/meeting-detection.ts` so it can be adjusted independently if Meet changes its DOM.

Open the panel with the Meeting Intelligence toolbar button while the Meet tab is active. That one click grants audio-only tab access and opens the persistent browser side panel; **Start listening** remains a separate explicit action inside the panel. The panel tracks the active tab and receives live capture-state updates while open. The in-meeting launcher was removed because it could open the panel without the browser's tab-capture grant.

## Capture pipeline

The side panel opens a temporary extension tab for Brave's microphone prompt when the user clicks **Enable microphone**. Choose “forever” in Brave’s Remember my decision selector to retain access across browser restarts; the current browser permission remains authoritative. The user then explicitly clicks **Start listening**. The service worker obtains a tab-capture stream ID for the authorized Meet tab, and the offscreen document consumes **audio only** from that tab alongside microphone audio. No screen-sharing picker, display capture, tab video, or camera stream is requested. The offscreen document owns the long-running `MediaRecorder` and WebSocket, so closing the side panel does not stop capture. Only a browser permission state explicitly reported as `denied` clears the remembered grant; a transient background reopen error does not.

The extension mixes Meet-tab audio and local microphone audio in the offscreen document and emits a self-contained `audio/webm;codecs=opus` recording every three seconds to `ws://localhost:8000/ws/meeting`. The endpoint is configured in `lib/config.ts`. The backend transcribes each bounded WebM/Opus window once, rather than retaining or retranscribing the entire meeting recording. The captured tab audio is also played back locally so the user can still hear the call.

The side panel shows the backend's newest-first results timeline. Task suggestions include a title, description, and explicit **Create Jira task** action. The user selects a Jira project, reviews or edits the task, and confirms creation; the card then links to the new issue. A board shows it if the board's filter includes that project. Spoken agreement in the meeting does not create an issue automatically. Search result cards identify Google Drive, Jira, or Confluence as the source and link to matching files, issues, or pages. Searches with no relevant results do not create timeline cards. While capture is running, the main panel shows capture status, microphone recovery notices, Stop listening, and the results timeline; transcript text remains in the local backend pipeline and is not rendered in the extension. The Settings button is always available and reveals Google Drive, Atlassian, and microphone details. Setup opens automatically when microphone permission is needed. Capture controls stay above the activity feed. The timeline scrolls independently and remains visible after capture stops while the user stays in the same Meet room. It is hidden in other tabs and cleared when that room ends or changes; starting a new capture also clears the previous results.

Capture stops when the user clicks Stop listening, the meeting tab closes, the tab leaves the meeting, or the media/WebSocket fails. The selected tab and microphone tracks are released before recorder shutdown waits. No full recording is accumulated or persisted by the extension.

During local development, click the reload icon for Meeting Intelligence in `chrome://extensions`, then refresh the Meet tab once so its content script reconnects to the updated extension.

The audio page checks microphone tracks and mixer state every three seconds,
resumes a suspended mixer, waits through brief mute signals, and tries to reopen a disconnected microphone. If
the microphone cannot reopen immediately, Meet-tab capture and the backend
WebSocket stay active so pending transcription, retrieval, and task work can
finish. Microphone recovery retries with bounded backoff instead of ending the
whole meeting capture.
Quiet input shows a compact notice. These checks do
not infer that silence is a fault; they explain what to check if you are speaking.
The browser's current permission state wins over a stale remembered hint. A
remembered grant is used only when the Permissions API is unavailable. A
transient automatic reopen error does not erase a grant; only an explicit
browser `denied` state clears it, and a busy or missing device does not.

The Meet content script also detects Meet's own “Microphone problem” control.
The side panel then explains that the site permission must be allowed, while
capture and pending backend work continue. The panel displays the extension
version so an unpacked-extension reload can be verified directly.

## Verification

Run `npm run typecheck` and `npm run test:session` before rebuilding. These tests cover capture recovery, permissions, content script reloads, meeting ownership, result contracts, and Jira requests. They are not included in the unpacked extension.

## Product messages and support codes

Use `lib/user-messages.ts` for every new error and recovery notice. Add a stable `MI-AREA-NNN` code and a plain-language action the person can take. Never display raw browser exceptions, API response bodies, HTTP status numbers, token configuration, or stack traces. `userError` at each error display preserves approved catalog messages and gives unknown errors a safe coded fallback. `httpProblem` translates sign-in expiry, access restrictions, and request limits. Keep diagnostic details in service logs.

Meeting Intelligence is positioned as an assistant for online meetings. Live listening currently works in Google Meet; other platforms are planned. Keep platform-specific guidance where it affects what users can do. Do not advertise other platforms as supported until their capture adapters exist.

Code areas: `APP` panel requests, `CAP` capture lifecycle, `CON` service connections, `MIC` microphone access/recovery, `DRV` Drive sign-in/search, `ATL` Jira/Confluence sign-in, `JIR` task creation, and `AUTH` account permissions. These codes remain stable when wording changes.

## Meeting recaps (0.1.11)

Settings offers **Live assistance** (default, recaps off), **Recap only**, and
**Live assistance + recap**. Preferences are user-scoped and fetched before
minting the short-lived tab stream ID. They do not open the microphone. The
server pins the mode for a recap session, including rejoins; the UI keeps an
active capture's mode separate from preferences for the next new meeting.

Capture UUID, Google Meet room code, and server recap session ID are separate.
Socket renewal/reconnect keeps the same microphone and capture UUID, replays
only unacknowledged audio, and includes the server recap ID. A new listening
segment in the same room can resume its recap during the ten-minute grace.

**Stop listening** immediately releases input hardware, drains a final bounded
recording window, and sends `end`. **Finish & email recap** uses `finish` after
the same drain. The retired drain uses local references and cannot mutate a
new capture. It waits up to 35 seconds for the exact final sequence acknowledgment;
an unavailable connection or missing final blob produces MI-MOM-003. Server
status remains authoritative, including when email delivery is uncertain.
Only metadata is retained for up to eight recap sessions; transcripts and audio
are not stored by this feature in browser storage. Sign-out clears this metadata.

The production package preserves its existing public manifest key. Preserve
that key and the installed folder when updating to retain the extension ID,
sign-in and microphone permission. If Reload stalls, use Load unpacked on the
same folder without removing the existing entry.
