# Meeting Intelligence Chrome extension

Meeting Intelligence captures Google Meet audio with the user's permission and streams it to the local backend. The backend transcribes and analyzes each audio window; the extension displays its search results and task suggestions.

## Local development

```bash
npm install
npm run dev
```

For a production build, run `npm run build`, then load `extension/.output/chrome-mv3` as an unpacked extension in `chrome://extensions`.

The extension detects an entered Meet call by combining a Meet meeting URL with call controls such as “Leave call” or microphone/camera controls. Detection is isolated in `lib/meeting-detection.ts` so it can be adjusted independently if Meet changes its DOM.

Open the panel with the Meeting Intelligence toolbar button while the Meet tab is active. That one click grants audio-only tab access and opens the persistent browser side panel; **Start capture** remains a separate explicit action inside the panel. The panel tracks the active tab and receives live capture-state updates while open. The in-meeting launcher was removed because it could open the panel without the browser's tab-capture grant.

## Capture pipeline

The side panel opens a temporary extension tab for Brave's microphone prompt when the user clicks **Enable microphone**. Once access succeeds, the extension remembers it across side-panel openings and browser restarts. The user then explicitly clicks **Start capture**. The service worker obtains a tab-capture stream ID for the authorized Meet tab, and the offscreen document consumes **audio only** from that tab alongside microphone audio. No screen-sharing picker, display capture, tab video, or camera stream is requested. The offscreen document owns the long-running `MediaRecorder` and WebSocket, so closing the side panel does not stop capture. If a later microphone capture is denied, the saved access state is cleared so the user can retry permission.

The extension mixes Meet-tab audio and local microphone audio in the offscreen document and emits a self-contained `audio/webm;codecs=opus` recording every three seconds to `ws://localhost:8000/ws/meeting`. The endpoint is configured in `lib/config.ts`. The backend transcribes each bounded WebM/Opus window once, rather than retaining or retranscribing the entire meeting recording. The captured tab audio is also played back locally so the user can still hear the call.

The side panel shows the backend's newest-first results timeline. Task suggestions include a title, description, and explicit **Create Jira task** action. The user selects a Jira project, reviews or edits the task, and confirms creation; the card then links to the new issue. A board shows it if the board's filter includes that project. Spoken agreement in the meeting does not create an issue automatically. Search result cards identify Google Drive, Jira, or Confluence as the source and link to matching files, issues, or pages. Searches with no relevant results do not create timeline cards. While capture is running, the main panel shows the capture status, Stop capture, live transcript, and timeline; the Settings button reveals Google Drive, Atlassian, tab audio, and microphone details. The timeline scrolls independently and remains visible after capture stops while the user stays in the same Meet room. It is hidden in other tabs and cleared when that room ends or changes; starting a new capture also clears the previous results.

Capture stops when the user clicks Stop, the meeting tab closes, the tab leaves the meeting, or the media/WebSocket fails. The selected tab and microphone tracks are released before recorder shutdown waits. No full recording is accumulated or persisted by the extension.

During local development, click the reload icon for Meeting Intelligence in `chrome://extensions`, then refresh the Meet tab once so its content script reconnects to the updated extension.

The live transcript retains the latest 3,000 characters for the current capture.
The audio page checks microphone tracks and mixer state every three seconds,
resumes a suspended mixer, and tries to reopen a disconnected microphone. If
the microphone cannot reopen immediately, Meet-tab capture and the backend
WebSocket stay active so pending transcription, retrieval, and task work can
finish. Microphone recovery retries with bounded backoff instead of ending the
whole meeting capture.
Quiet input and recognition failures show separate notices. These checks do
not infer that silence is a fault; they explain what to check if you are speaking.
A side-panel permission query reporting `prompt` does not erase a previously
successful grant. Actual capture denial or explicit browser denial clears it;
a busy or missing device does not.
