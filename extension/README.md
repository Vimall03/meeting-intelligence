# Meeting Intelligence Chrome extension

This is the capture-only phase of Meeting Intelligence. It supports Google Meet and does not transcribe, analyze, store, or interpret meeting content.

## Local development

```bash
npm install
npm run dev
```

For a production build, run `npm run build`, then load `extension/.output/chrome-mv3` as an unpacked extension in `chrome://extensions`.

The extension detects an entered Meet call by combining a Meet meeting URL with call controls such as “Leave call” or microphone/camera controls. Detection is isolated in `lib/meeting-detection.ts` so it can be adjusted independently if Meet changes its DOM.

The toolbar button authorizes audio-only access for the current Meet tab and opens a persistent, responsive browser side panel. The side panel tracks the active tab and receives live capture-state updates while open; it does not need to be closed and reopened to refresh. A compact **Meeting Intelligence** launcher also appears in an active Meet call. Neither surface starts capture automatically. Opening the panel from the in-page launcher does not grant tab audio access; click the toolbar icon once on that Meet tab before starting.

## Capture pipeline

The side panel opens a temporary extension tab for Brave's microphone prompt when the user clicks **Enable microphone**. Once access succeeds, the extension remembers it across side-panel openings and browser restarts. The user then explicitly clicks **Start capture**. The service worker obtains a tab-capture stream ID for the toolbar-authorized Meet tab, and the offscreen document consumes **audio only** from that tab alongside microphone audio. No screen-sharing picker, display capture, tab video, or camera stream is requested. The offscreen document owns the long-running `MediaRecorder` and WebSocket, so closing the side panel does not stop capture. If a later microphone capture is denied, the saved access state is cleared so the user can retry permission.

The extension mixes Meet-tab audio and local microphone audio in the offscreen document and emits `audio/webm;codecs=opus` in two-second binary WebSocket messages to `ws://localhost:8000/ws/meeting`. The endpoint is configured in `lib/config.ts`. The backend receives a single mixed audio stream and should treat the messages as WebM/Opus media chunks; this phase intentionally sends no transcript or meeting metadata. The captured tab audio is also played back locally so the user can still hear the call.

Capture stops when the user clicks Stop, the meeting tab closes, the tab leaves the meeting, or the media/WebSocket fails. The selected tab and microphone tracks are released before recorder shutdown waits. No full recording is accumulated or persisted by the extension.

During local development, click the reload icon for Meeting Intelligence in `chrome://extensions`, then refresh the Meet tab once so its content script reconnects to the updated extension.
