import { AUDIO_MIME_TYPE, AUDIO_WINDOW_MS, MEETING_SOCKET_URL } from '../lib/config';
import { isTimelineSnapshot } from '../lib/timeline';

let recorder: MediaRecorder | undefined;
let tabStream: MediaStream | undefined;
let microphoneStream: MediaStream | undefined;
let recordingStream: MediaStream | undefined;
let socket: WebSocket | undefined;
let audioContext: AudioContext | undefined;
let tabSource: MediaStreamAudioSourceNode | undefined;
let microphoneSource: MediaStreamAudioSourceNode | undefined;
let tabGain: GainNode | undefined;
let microphoneGain: GainNode | undefined;
let recordingWindowTimer: ReturnType<typeof setTimeout> | undefined;
let audioHealthTimer: ReturnType<typeof setInterval> | undefined;
let stopping = false;
let captureGeneration = 0;
let captureSessionId: string | undefined;
let startPending = false;

function assertCurrent(generation: number) {
  if (generation !== captureGeneration || stopping) {
    throw new DOMException('Capture was canceled.', 'AbortError');
  }
}

function startRecordingWindow(activeRecorder: MediaRecorder) {
  if (recorder !== activeRecorder || stopping) return;
  activeRecorder.start();
  recordingWindowTimer = setTimeout(() => {
    if (recorder === activeRecorder && !stopping && activeRecorder.state === 'recording') {
      activeRecorder.stop();
    }
  }, AUDIO_WINDOW_MS);
}

async function start(streamId: string, sessionId: string) {
  // A tabCapture stream ID can be consumed in this extension offscreen page.
  // Request audio alone: no tab video or display-capture permission is used.
  const selection = navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: 'tab',
        chromeMediaSourceId: streamId,
      },
    } as MediaTrackConstraints,
    video: false,
  });
  void selection.catch(() => undefined);
  const cleanup = stop();
  const generation = ++captureGeneration;
  await cleanup;
  if (generation !== captureGeneration) {
    void selection.then((stream) => stream.getTracks().forEach((track) => track.stop()));
    throw new DOMException('Capture was canceled.', 'AbortError');
  }
  stopping = false;
  captureSessionId = sessionId;
  const selectedTab = await selection;
  if (generation !== captureGeneration || stopping) {
    selectedTab.getTracks().forEach((track) => track.stop());
    throw new DOMException('Capture was canceled.', 'AbortError');
  }
  tabStream = selectedTab;
  if (tabStream.getAudioTracks().length === 0) {
    throw new Error('Brave did not provide audio from this Meet tab.');
  }
  if (tabStream.getVideoTracks().length > 0) {
    throw new Error('Brave unexpectedly provided a video track; capture stopped to protect tab privacy.');
  }

  // The tab stream contains other participants and shared media. The local
  // speaker's voice is not played back into the tab, so capture the mic
  // separately and mix both sources into one WebM/Opus stream.
  try {
    const selectedMicrophone = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    if (generation !== captureGeneration || stopping) {
      selectedMicrophone.getTracks().forEach((track) => track.stop());
      throw new DOMException('Capture was canceled.', 'AbortError');
    }
    microphoneStream = selectedMicrophone;
    await chrome.runtime.sendMessage({ type: 'MICROPHONE_PERMISSION_STATUS', granted: true }).catch(() => undefined);
    assertCurrent(generation);
  } catch (error) {
    if (error instanceof Error && (error.name === 'NotAllowedError' || error.name === 'SecurityError')) {
      const permission = await navigator.permissions.query({ name: 'microphone' as PermissionName }).catch(() => undefined);
      if (permission?.state !== 'granted') {
        await chrome.runtime.sendMessage({ type: 'MICROPHONE_PERMISSION_STATUS', granted: false }).catch(() => undefined);
        throw new Error('Brave has not granted microphone access to Meeting Intelligence. Press Enable microphone in the side panel, then allow the browser prompt.');
      }
      throw new Error(`Brave could not open the microphone despite permission being granted (${error.name}: ${error.message}). Check the selected input device and macOS microphone access.`);
    }
    throw error;
  }

  if (!MediaRecorder.isTypeSupported(AUDIO_MIME_TYPE)) {
    throw new Error('This browser does not support WebM/Opus audio capture.');
  }

  notify({ captureStatus: 'connecting', backendConnected: false, timeline: undefined, error: undefined });
  const sessionSocket = new WebSocket(MEETING_SOCKET_URL);
  socket = sessionSocket;
  sessionSocket.onopen = () => {
    if (socket !== sessionSocket || stopping) return;
    notify({ captureStatus: 'capturing', backendConnected: true, error: undefined });
  };
  sessionSocket.onerror = () => {
    if (socket !== sessionSocket || stopping) return;
    notify({ captureStatus: 'error', backendConnected: false, error: 'Backend unavailable.' });
    void stop();
  };
  sessionSocket.onclose = () => {
    if (socket !== sessionSocket || stopping) return;
    notify({ captureStatus: 'error', backendConnected: false, error: 'Backend disconnected.' });
    // Release the selected tab and mic when the backend disconnects.
    void stop();
  };
  sessionSocket.onmessage = (event) => {
    if (socket !== sessionSocket || stopping || typeof event.data !== 'string') return;
    try {
      const message: unknown = JSON.parse(event.data);
      if (isTimelineSnapshot(message)) notify({ timeline: message });
      if (typeof message === 'object' && message !== null && 'type' in message && message.type === 'transcription_state') {
        const update = message as { transcript?: unknown; warning?: unknown };
        if (typeof update.transcript === 'string') notify({
          transcript: update.transcript.slice(-3000),
          transcriptionWarning: typeof update.warning === 'string' ? update.warning : undefined,
        });
      }
    } catch {
      // Ignore malformed or unrelated backend messages.
    }
  };

  audioContext = new AudioContext();
  await audioContext.resume();
  assertCurrent(generation);
  const mix = audioContext.createMediaStreamDestination();
  tabSource = audioContext.createMediaStreamSource(tabStream);
  microphoneSource = audioContext.createMediaStreamSource(microphoneStream);
  tabGain = audioContext.createGain();
  microphoneGain = audioContext.createGain();
  tabGain.gain.value = 0.7;
  microphoneGain.gain.value = 0.7;

  tabSource.connect(tabGain);
  tabGain.connect(mix);
  // tabCapture mutes the original tab playback while recording. Restore it
  // so the meeting remains audible to the user.
  tabSource.connect(audioContext.destination);
  microphoneSource.connect(microphoneGain);
  microphoneGain.connect(mix);
  recordingStream = mix.stream;
  const sessionContext = audioContext;
  const microphoneAnalyser = sessionContext.createAnalyser();
  const tabAnalyser = sessionContext.createAnalyser();
  microphoneAnalyser.fftSize = tabAnalyser.fftSize = 1024;
  microphoneSource.connect(microphoneAnalyser);
  tabSource.connect(tabAnalyser);
  const samples = new Float32Array(1024);
  const level = (analyser: AnalyserNode) => {
    analyser.getFloatTimeDomainData(samples);
    return Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
  };
  let quietSince = Date.now();
  let recoveringMicrophone = false;
  let microphoneRecoveryAttempts = 0;
  let nextMicrophoneRecoveryAt = 0;
  const recoverMicrophone = async () => {
    const now = Date.now();
    if (
      recoveringMicrophone
      || stopping
      || generation !== captureGeneration
      || now < nextMicrophoneRecoveryAt
    ) return;
    recoveringMicrophone = true;
    notify({ audioWarning: 'Microphone unavailable. Meet audio and searches are still running while it reconnects…' });
    try {
      const replacement = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false,
      });
      if (stopping || generation !== captureGeneration) {
        replacement.getTracks().forEach((track) => track.stop());
        return;
      }
      microphoneSource?.disconnect();
      microphoneStream?.getTracks().forEach((track) => track.stop());
      microphoneStream = replacement;
      microphoneSource = sessionContext.createMediaStreamSource(replacement);
      microphoneSource.connect(microphoneGain!);
      microphoneSource.connect(microphoneAnalyser);
      quietSince = Date.now();
      microphoneRecoveryAttempts = 0;
      nextMicrophoneRecoveryAt = 0;
      await chrome.runtime.sendMessage({ type: 'MICROPHONE_PERMISSION_STATUS', granted: true }).catch(() => undefined);
      notify({ audioWarning: undefined });
    } catch (error) {
      if (stopping || generation !== captureGeneration) return;
      microphoneRecoveryAttempts += 1;
      const retryDelayMs = Math.min(30_000, 3_000 * 2 ** (microphoneRecoveryAttempts - 1));
      nextMicrophoneRecoveryAt = Date.now() + retryDelayMs;
      if (error instanceof Error && ['NotAllowedError', 'SecurityError'].includes(error.name)) {
        await chrome.runtime.sendMessage({ type: 'MICROPHONE_PERMISSION_STATUS', granted: false }).catch(() => undefined);
      }
      const reason = error instanceof Error && error.name
        ? ` (${error.name})`
        : '';
      notify({
        audioWarning: `Microphone unavailable${reason}. Meet audio, transcription, and searches are still running; retrying automatically.`,
      });
    } finally {
      recoveringMicrophone = false;
    }
  };
  // A recording can continue producing silent WebM windows when its input
  // ends or Web Audio is suspended. Check the actual sources, not just recorder.state.
  audioHealthTimer = setInterval(() => {
    if (stopping || generation !== captureGeneration) return;
    const tabTracks = tabStream?.getAudioTracks() ?? [];
    if (tabTracks.length === 0 || tabTracks.every((track) => track.readyState === 'ended')) {
      notify({ captureStatus: 'error', backendConnected: false, error: 'Meet tab audio ended. Restart capture in the active call.' });
      void stop();
      return;
    }
    const microphoneTracks = microphoneStream?.getAudioTracks() ?? [];
    const microphoneUnavailable = microphoneStream?.active !== true || microphoneTracks.length === 0 || microphoneTracks.every(
      (track) => track.readyState === 'ended' || track.muted || !track.enabled,
    );
    if (microphoneUnavailable) {
      void recoverMicrophone();
      return;
    }
    if (sessionContext.state !== 'running') {
      void sessionContext.resume().catch(() => undefined);
      notify({ audioWarning: 'Audio input paused. Trying to resume…' });
      return;
    }
    if (recoveringMicrophone) return;
    const hasAudio = level(microphoneAnalyser) > 0.001 || level(tabAnalyser) > 0.001;
    if (hasAudio) quietSince = Date.now();
    notify({ audioWarning: Date.now() - quietSince > 15000
      ? 'No audio detected. If you are speaking, check the microphone selected in Brave.'
      : undefined });
  }, 3000);

  const activeRecorder = new MediaRecorder(recordingStream, { mimeType: AUDIO_MIME_TYPE });
  recorder = activeRecorder;
  activeRecorder.ondataavailable = (event) => {
    if (recorder === activeRecorder && event.data.size > 0 && sessionSocket.readyState === WebSocket.OPEN) sessionSocket.send(event.data);
  };
  activeRecorder.onstop = () => {
    if (recordingWindowTimer) clearTimeout(recordingWindowTimer);
    recordingWindowTimer = undefined;
    startRecordingWindow(activeRecorder);
  };
  activeRecorder.onerror = () => {
    if (recorder !== activeRecorder) return;
    notify({ captureStatus: 'error', backendConnected: false, error: 'Audio capture failed.' });
    void stop();
  };
  startRecordingWindow(activeRecorder);
}

async function stop() {
  captureGeneration += 1;
  stopping = true;
  if (recordingWindowTimer) clearTimeout(recordingWindowTimer);
  recordingWindowTimer = undefined;
  if (audioHealthTimer) clearInterval(audioHealthTimer);
  audioHealthTimer = undefined;
  const activeRecorder = recorder;
  recorder = undefined;

  // Release the selected tab and mic before waiting for MediaRecorder to stop.
  recordingStream?.getTracks().forEach((track) => track.stop());
  tabStream?.getTracks().forEach((track) => track.stop());
  microphoneStream?.getTracks().forEach((track) => track.stop());

  if (activeRecorder && activeRecorder.state !== 'inactive') {
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const recorderStopped = new Promise<void>((resolve) => {
      const finish = () => {
        if (timeout) clearTimeout(timeout);
        resolve();
      };
      activeRecorder.addEventListener('stop', finish, { once: true });
      activeRecorder.addEventListener('error', finish, { once: true });
      timeout = setTimeout(finish, 750);
    });
    try {
      activeRecorder.stop();
    } catch {
      // Tracks are already released; a recorder that stopped concurrently
      // must not prevent cleanup or a later capture attempt.
    }
    await recorderStopped;
  }
  tabSource?.disconnect();
  microphoneSource?.disconnect();
  tabGain?.disconnect();
  microphoneGain?.disconnect();
  tabSource = undefined;
  microphoneSource = undefined;
  tabGain = undefined;
  microphoneGain = undefined;
  const activeAudioContext = audioContext;
  audioContext = undefined;
  try { await activeAudioContext?.close(); } catch { /* Context may already be closed. */ }
  recordingStream = undefined;
  tabStream = undefined;
  microphoneStream = undefined;
  socket?.close();
  socket = undefined;
}

function notify(state: Record<string, unknown>) {
  chrome.runtime.sendMessage({ type: 'CAPTURE_STATE', captureSessionId, state }).catch(() => undefined);
}

chrome.runtime.onMessage.addListener((message: { type: string; streamId?: string; captureSessionId?: string }, _sender, sendResponse) => {
  if (message.type === 'GET_OFFSCREEN_STATUS') {
    sendResponse({ active: !stopping && tabStream?.active === true });
    return false;
  }
  if (message.type === 'START_OFFSCREEN') {
    if (!message.streamId || !message.captureSessionId) {
      sendResponse({ ok: false, error: 'Missing tab audio authorization.' });
      return false;
    }
    if (startPending) {
      sendResponse({ ok: false, error: 'Audio capture is already starting. Wait a moment before retrying.' });
      return false;
    }
    startPending = true;
    start(message.streamId, message.captureSessionId)
      .then(() => sendResponse({ ok: true }))
      .catch(async (error: unknown) => {
        const description = describeCaptureError(error);
        if (!(error instanceof Error && error.name === 'AbortError')) {
          await stop();
          notify({ captureStatus: 'error', backendConnected: false, error: description });
        }
        sendResponse({ ok: false, error: description });
      }).finally(() => { startPending = false; });
    return true;
  }
  if (message.type === 'STOP_OFFSCREEN') {
    stop().then(() => sendResponse({ ok: true }));
    return true;
  }
});

function describeCaptureError(error: unknown) {
  const errorName = error instanceof Error ? error.name : 'UnknownError';
  if (errorName === 'NotFoundError') {
    return 'No microphone was found on this device.';
  }
  return error instanceof Error ? error.message : 'Unable to capture meeting and microphone audio.';
}

export default defineUnlistedScript(() => {
  // The offscreen page's listeners are registered when this module loads.
});
