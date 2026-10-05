import { problem, userError, microphoneProblem } from '../lib/user-messages';
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
let authTimer: ReturnType<typeof setTimeout> | undefined;
let socketAuthorized = false;
let authorizedUntil = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
let heartbeatTimer: ReturnType<typeof setInterval> | undefined;
let reconnectStartedAt = 0;
let reconnectAttempt = 0;
let audioSequence = 0;
let acknowledgedSequence = 0;
let audioQueue: { sequence: number; data: Blob }[] = [];
let sentThrough = 0;
let lastServerMessageAt = 0;

function flushAudio() {
  const activeSocket = socket;
  if (!activeSocket || activeSocket.readyState !== WebSocket.OPEN || !socketAuthorized) return;
  try {
    for (const window of audioQueue) {
      if (window.sequence <= sentThrough) continue;
      if ((activeSocket.bufferedAmount || 0) > 4_000_000) break;
      activeSocket.send(JSON.stringify({ type: 'audio', sequence: window.sequence }));
      activeSocket.send(window.data);
      sentThrough = window.sequence;
    }
  } catch {
    retryConnection(activeSocket, captureGeneration);
  }
}

function retryConnection(failedSocket: WebSocket | undefined, generation: number) {
  if (generation !== captureGeneration || stopping || (failedSocket && socket !== failedSocket)) return;
  if (authTimer) clearTimeout(authTimer);
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  authTimer = heartbeatTimer = undefined;
  socket = undefined;
  socketAuthorized = false;
  failedSocket?.close();
  notify({ captureStatus: 'connecting', backendConnected: false, error: problem('MI-CON-004') });
  if (!reconnectStartedAt) reconnectStartedAt = Date.now();
  if (Date.now() - reconnectStartedAt >= 60_000) {
    notify({ captureStatus: 'error', backendConnected: false, error: problem('MI-CON-005') });
    void stop();
    return;
  }
  if (reconnectTimer) return;
  const delay = Math.min(10_000, 1000 * 2 ** Math.min(reconnectAttempt++, 4)) + Math.floor(Math.random() * 250);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = undefined;
    void connectSocket(generation).catch(error => {
      if (generation !== captureGeneration || stopping) return;
      if (error instanceof Error && error.message === problem('MI-AUTH-105')) {
        notify({ captureStatus: 'error', backendConnected: false, error: problem('MI-AUTH-105') });
        void stop();
      } else retryConnection(undefined, generation);
    });
  }, delay);
}

async function connectSocket(generation: number) {
  const authorization = await chrome.runtime.sendMessage({ type: 'MEETING_TICKET' }) as { ok?: boolean; ticket?: string; error?: string };
  assertCurrent(generation);
  if (!authorization?.ok || !authorization.ticket) throw new Error(authorization?.error ?? problem('MI-AUTH-104'));
  const sessionSocket = new WebSocket(MEETING_SOCKET_URL);
  socket = sessionSocket;
  const handshakeTimeout = () => retryConnection(sessionSocket, generation);
  authTimer = setTimeout(handshakeTimeout, 10_000);
  sessionSocket.onopen = () => {
    if (socket !== sessionSocket || stopping) return;
    sessionSocket.send(JSON.stringify({ type: 'authenticate', ticket: authorization.ticket, meetingId: captureSessionId, lastAck: acknowledgedSequence }));
  };
  sessionSocket.onerror = () => retryConnection(sessionSocket, generation);
  sessionSocket.onclose = event => {
    if (socket !== sessionSocket || stopping) return;
    if (event.code === 4401 || event.code === 4409) {
      notify({ captureStatus: 'error', backendConnected: false, error: problem(event.code === 4401 ? 'MI-AUTH-105' : 'MI-CON-007') });
      void stop();
    } else retryConnection(sessionSocket, generation);
  };
  sessionSocket.onmessage = event => {
    if (socket !== sessionSocket || stopping || typeof event.data !== 'string') return;
    try {
      const message = JSON.parse(event.data);
      lastServerMessageAt = Date.now();
      if (message.type === 'auth_ready' && typeof message.expiresAt === 'number') {
        const firstAuthorization = !socketAuthorized;
        socketAuthorized = true;
        authorizedUntil = typeof message.expiresIn === 'number' ? Date.now() + message.expiresIn * 1000 : message.expiresAt * 1000;
        if (authTimer) clearTimeout(authTimer);
        authTimer = setTimeout(() => { void renewSocket(sessionSocket, generation); }, Math.max(1000, authorizedUntil - Date.now() - 120_000));
        if (firstAuthorization) {
          reconnectStartedAt = reconnectAttempt = 0;
          sentThrough = acknowledgedSequence;
          if (heartbeatTimer) clearInterval(heartbeatTimer);
          heartbeatTimer = setInterval(() => {
            if (socket !== sessionSocket || stopping) return;
            if (Date.now() - lastServerMessageAt > 35_000) { retryConnection(sessionSocket, generation); return; }
            try { sessionSocket.send(JSON.stringify({ type: 'ping' })); flushAudio(); }
            catch { retryConnection(sessionSocket, generation); }
          }, 15_000);
        }
        if (Number.isSafeInteger(message.ack) && message.ack >= acknowledgedSequence && message.ack <= audioSequence) {
          acknowledgedSequence = message.ack;
          audioQueue = audioQueue.filter(window => window.sequence > acknowledgedSequence);
          sentThrough = Math.max(sentThrough, acknowledgedSequence);
        }
        notify({ captureStatus: 'capturing', backendConnected: true, error: undefined });
        flushAudio();
      } else if (socketAuthorized && message.type === 'audio_ack' && Number.isSafeInteger(message.sequence)
        && message.sequence >= acknowledgedSequence && message.sequence <= audioSequence) {
        acknowledgedSequence = message.sequence;
        audioQueue = audioQueue.filter(window => window.sequence > acknowledgedSequence);
        flushAudio();
      } else if (socketAuthorized && isTimelineSnapshot(message)) notify({ timeline: message });
    } catch {
      // Ignore malformed or unrelated messages; heartbeat detects a stalled peer.
    }
  };
}

async function renewSocket(activeSocket: WebSocket, generation: number) {
  try {
    const result = await chrome.runtime.sendMessage({ type: 'MEETING_TICKET' }) as { ok?: boolean; ticket?: string; error?: string };
    assertCurrent(generation);
    if (socket !== activeSocket || activeSocket.readyState !== WebSocket.OPEN) return;
    if (!result?.ok || !result.ticket) throw new Error(result?.error ?? problem('MI-AUTH-104'));
    activeSocket.send(JSON.stringify({ type: 'authenticate', ticket: result.ticket }));
    authTimer = setTimeout(() => { void renewSocket(activeSocket, generation); }, 10_000);
  } catch (error) {
    if (generation !== captureGeneration || stopping || socket !== activeSocket) return;
    if (Date.now() >= authorizedUntil || (error instanceof Error && error.message === problem('MI-AUTH-105'))) {
      notify({ captureStatus: 'error', backendConnected: false, error: problem('MI-AUTH-105') });
      await stop();
    } else {
      authTimer = setTimeout(() => { void renewSocket(activeSocket, generation); }, 10_000);
    }
  }
}

function assertCurrent(generation: number) {
  if (generation !== captureGeneration || stopping) {
    throw new DOMException(problem('MI-CAP-006'), 'AbortError');
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
    throw new DOMException(problem('MI-CAP-006'), 'AbortError');
  }
  stopping = false;
  captureSessionId = sessionId;
  const selectedTab = await selection;
  if (generation !== captureGeneration || stopping) {
    selectedTab.getTracks().forEach((track) => track.stop());
    throw new DOMException(problem('MI-CAP-006'), 'AbortError');
  }
  tabStream = selectedTab;
  if (tabStream.getAudioTracks().length === 0) {
    throw new Error(problem('MI-CAP-003'));
  }
  if (tabStream.getVideoTracks().length > 0) {
    throw new Error(problem('MI-CAP-009'));
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
      throw new DOMException(problem('MI-CAP-006'), 'AbortError');
    }
    microphoneStream = selectedMicrophone;
    await chrome.runtime.sendMessage({ type: 'MICROPHONE_PERMISSION_STATUS', granted: true }).catch(() => undefined);
    assertCurrent(generation);
  } catch (error) {
    if (error instanceof Error && (error.name === 'NotAllowedError' || error.name === 'SecurityError')) {
      const permission = await navigator.permissions.query({ name: 'microphone' as PermissionName }).catch(() => undefined);
      if (permission?.state !== 'granted') {
        await chrome.runtime.sendMessage({ type: 'MICROPHONE_PERMISSION_STATUS', granted: false }).catch(() => undefined);
        throw new Error(problem('MI-MIC-001'));
      }
      throw new Error(problem('MI-MIC-002'));
    }
    throw error;
  }

  if (!MediaRecorder.isTypeSupported(AUDIO_MIME_TYPE)) {
    throw new Error(problem('MI-CAP-008'));
  }

  notify({ captureStatus: 'connecting', backendConnected: false, timeline: undefined, error: undefined });
  await connectSocket(generation);

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
  let microphoneUnavailableChecks = 0;
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
    notify({ audioWarning: problem('MI-MIC-004') });
    try {
      // Release the unusable source before opening its replacement. Brave can
      // reject a second request while a stale live-but-muted track still owns
      // the input device.
      microphoneSource?.disconnect();
      microphoneStream?.getTracks().forEach((track) => track.stop());
      microphoneSource = undefined;
      microphoneStream = undefined;
      const replacement = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false,
      });
      if (stopping || generation !== captureGeneration) {
        replacement.getTracks().forEach((track) => track.stop());
        return;
      }
      microphoneStream = replacement;
      microphoneSource = sessionContext.createMediaStreamSource(replacement);
      microphoneSource.connect(microphoneGain!);
      microphoneSource.connect(microphoneAnalyser);
      quietSince = Date.now();
      microphoneRecoveryAttempts = 0;
      microphoneUnavailableChecks = 0;
      nextMicrophoneRecoveryAt = 0;
      await chrome.runtime.sendMessage({ type: 'MICROPHONE_PERMISSION_STATUS', granted: true }).catch(() => undefined);
      if (stopping || generation !== captureGeneration) return;
      notify({ audioWarning: undefined });
    } catch (error) {
      if (stopping || generation !== captureGeneration) return;
      microphoneRecoveryAttempts += 1;
      const retryDelayMs = Math.min(30_000, 3_000 * 2 ** (microphoneRecoveryAttempts - 1));
      nextMicrophoneRecoveryAt = Date.now() + retryDelayMs;
      let permissionState: PermissionState | undefined;
      if (error instanceof Error && ['NotAllowedError', 'SecurityError'].includes(error.name)) {
        permissionState = await navigator.permissions.query({ name: 'microphone' as PermissionName })
          .then((permission) => permission.state)
          .catch(() => undefined);
      }
      if (stopping || generation !== captureGeneration) return;
      // A transient NotAllowedError from an automatic background reopen is
      // not proof that the user revoked a previously granted permission.
      if (permissionState === 'denied') {
        await chrome.runtime.sendMessage({ type: 'MICROPHONE_PERMISSION_STATUS', granted: false }).catch(() => undefined);
      }
      if (stopping || generation !== captureGeneration) return;
      notify({
        audioWarning: permissionState === 'denied'
          ? problem('MI-MIC-001')
          : problem('MI-MIC-006'),
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
      notify({ captureStatus: 'error', backendConnected: false, error: problem('MI-CAP-007') });
      void stop();
      return;
    }
    if (sessionContext.state !== 'running') {
      void sessionContext.resume().catch(() => undefined);
      notify({ audioWarning: problem('MI-MIC-007') });
    }
    const microphoneTracks = microphoneStream?.getAudioTracks() ?? [];
    const microphoneEnded = microphoneStream?.active !== true || microphoneTracks.length === 0
      || microphoneTracks.every((track) => track.readyState === 'ended');
    if (microphoneEnded) {
      microphoneUnavailableChecks = 0;
      void recoverMicrophone();
      return;
    }
    const microphonePaused = microphoneTracks.every((track) => track.muted || !track.enabled);
    if (microphonePaused) {
      microphoneUnavailableChecks += 1;
      if (microphoneUnavailableChecks >= 3) void recoverMicrophone();
      else notify({ audioWarning: problem('MI-MIC-005') });
      return;
    }
    microphoneUnavailableChecks = 0;
    if (recoveringMicrophone) return;
    const hasAudio = level(microphoneAnalyser) > 0.001 || level(tabAnalyser) > 0.001;
    if (hasAudio) quietSince = Date.now();
    notify({ audioWarning: Date.now() - quietSince > 15000
      ? problem('MI-MIC-008')
      : undefined });
  }, 3000);

  const activeRecorder = new MediaRecorder(recordingStream, { mimeType: AUDIO_MIME_TYPE });
  recorder = activeRecorder;
  activeRecorder.ondataavailable = (event) => {
    if (recorder !== activeRecorder || stopping || !event.data.size) return;
    audioQueue.push({ sequence: ++audioSequence, data: event.data });
    if (event.data.size > 2_000_000 || audioQueue.length > 20 || audioQueue.reduce((size, window) => size + window.data.size, 0) > 12_000_000) {
      notify({ captureStatus: 'error', backendConnected: false, error: problem('MI-CON-005') });
      void stop();
      return;
    }
    flushAudio();
  };
  activeRecorder.onstop = () => {
    if (recordingWindowTimer) clearTimeout(recordingWindowTimer);
    recordingWindowTimer = undefined;
    startRecordingWindow(activeRecorder);
  };
  activeRecorder.onerror = () => {
    if (recorder !== activeRecorder) return;
    notify({ captureStatus: 'error', backendConnected: false, error: problem('MI-CAP-004') });
    void stop();
  };
  startRecordingWindow(activeRecorder);
}

async function stop() {
  captureGeneration += 1;
  stopping = true;
  socketAuthorized = false;
  authorizedUntil = 0;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  reconnectTimer = heartbeatTimer = undefined;
  reconnectStartedAt = reconnectAttempt = 0;
  audioSequence = acknowledgedSequence = sentThrough = 0;
  audioQueue = [];
  if (authTimer) clearTimeout(authTimer);
  authTimer = undefined;
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
  if (_sender.tab) return false;
  if (message.type === 'GET_OFFSCREEN_STATUS') {
    sendResponse({ active: !stopping && tabStream?.active === true });
    return false;
  }
  if (message.type === 'START_OFFSCREEN') {
    if (!message.streamId || !message.captureSessionId) {
      sendResponse({ ok: false, error: problem('MI-CAP-003') });
      return false;
    }
    if (startPending) {
      sendResponse({ ok: false, error: problem('MI-CAP-002') });
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
  if (['NotFoundError', 'NotReadableError', 'NotAllowedError', 'SecurityError', 'OverconstrainedError'].includes(errorName)) {
    return microphoneProblem(errorName);
  }
  return userError(error, 'MI-CAP-004');
}

export default defineUnlistedScript(() => {
  // The offscreen page's listeners are registered when this module loads.
});
