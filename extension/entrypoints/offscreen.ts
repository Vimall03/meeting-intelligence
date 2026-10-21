import { problem, userError, microphoneProblem } from '../lib/user-messages';
import { MEETING_SOCKET_URL } from '../lib/config';
import { PcmCapture } from '../lib/pcm-capture';
import { PcmReplay, type PcmFrame } from '../lib/pcm-replay';
import type { MomMode, MomMeeting } from '../lib/mom';
import { isTimelineSnapshot } from '../lib/timeline';

let pcmCapture: PcmCapture | undefined;
let tabStream: MediaStream | undefined;
let microphoneStream: MediaStream | undefined;
let recordingStream: MediaStream | undefined;
let socket: WebSocket | undefined;
let audioContext: AudioContext | undefined;
let tabSource: MediaStreamAudioSourceNode | undefined;
let microphoneSource: MediaStreamAudioSourceNode | undefined;
let tabGain: GainNode | undefined;
let microphoneGain: GainNode | undefined;
let audioHealthTimer: ReturnType<typeof setInterval> | undefined;
let stopping = false;
let captureGeneration = 0;
let captureSessionId: string | undefined;
let startPending = false;
let roomId: string | undefined;
let captureUserId: string | undefined;
let captureMode: MomMode = 'live_only';
let minutesSession: MomMeeting | undefined;
let minutesGrace = 600;
let authTimer: ReturnType<typeof setTimeout> | undefined;
let socketAuthorized = false;
let authorizedUntil = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
let heartbeatTimer: ReturnType<typeof setInterval> | undefined;
let reconnectStartedAt = 0;
let reconnectAttempt = 0;
let replay = new PcmReplay();
let pumpTimer: ReturnType<typeof setInterval> | undefined;
let nextSendAt = 0;
let recoveryCount = 0;
let sentThrough = 0;
let lastServerMessageAt = 0;
let retryMicrophoneCapture: (() => void) | undefined;

function flushAudio() {
  const activeSocket = socket;
  if (!activeSocket || activeSocket.readyState !== WebSocket.OPEN || !socketAuthorized) return;
  try {
    const frame = replay.frames.find(frame => frame.end > sentThrough);
    if (!frame || Date.now() < nextSendAt || (activeSocket.bufferedAmount || 0) > 64_000) return;
    const start = Math.max(frame.start, sentThrough);
    activeSocket.send(JSON.stringify({ type: 'audio', startSample: start }));
    activeSocket.send(frame.data.slice((start-frame.start)*2));
    sentThrough = frame.end;
    // At most 2x real time while catching up; normal capture produces 5 frames/s.
    nextSendAt = Date.now() + 100;
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
  if (failedSocket) recoveryCount++;
  failedSocket?.close();
  notify({ captureStatus: 'connecting', backendConnected: false, error: problem('MI-CON-004') });
  if (!reconnectStartedAt) reconnectStartedAt = Date.now();
  if (Date.now() - reconnectStartedAt >= 60_000 || recoveryCount > 5) {
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
    sessionSocket.send(JSON.stringify({ type: 'authenticate', ticket: authorization.ticket, meetingId: captureSessionId, roomId, momSessionId: minutesSession?.sessionId, lastAck: replay.ack,
      audioFormat: 'pcm_s16le_16000_v2' }));
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
        if (message.audioFormat !== 'pcm_s16le_16000_v2') {
          retryConnection(sessionSocket, generation);
          return;
        }
        const firstAuthorization = !socketAuthorized;
        socketAuthorized = true;
        authorizedUntil = typeof message.expiresIn === 'number' ? Date.now() + message.expiresIn * 1000 : message.expiresAt * 1000;
        if (authTimer) clearTimeout(authTimer);
        authTimer = setTimeout(() => { void renewSocket(sessionSocket, generation); }, Math.max(1000, authorizedUntil - Date.now() - 120_000));
        if (firstAuthorization) {
          reconnectStartedAt = reconnectAttempt = 0;
          sentThrough = replay.ack;
          nextSendAt = 0;
          if (heartbeatTimer) clearInterval(heartbeatTimer);
          heartbeatTimer = setInterval(() => {
            if (socket !== sessionSocket || stopping) return;
            if (Date.now() - lastServerMessageAt > 35_000) { retryConnection(sessionSocket, generation); return; }
            try { sessionSocket.send(JSON.stringify({ type: 'ping' })); flushAudio(); }
            catch { retryConnection(sessionSocket, generation); }
          }, 15_000);
        }
        if (!replay.acknowledge(message.ack)) {
          notify({ captureStatus: 'error', backendConnected: false, error: problem('MI-CON-007') });
          void stop();
          return;
        }
        sentThrough = Math.max(sentThrough, replay.ack);
        notify({ captureStatus: 'capturing', backendConnected: true, error: undefined });
        flushAudio();
      } else if (socketAuthorized && message.type === 'audio_ack') {
        if (replay.acknowledge(message.sequence)) flushAudio();
      } else if (socketAuthorized && message.type === 'mom_session' && /^[a-f0-9]{32}$/.test(message.sessionId)
        && ['mom_only', 'both'].includes(message.mode) && roomId && captureSessionId) {
        minutesGrace = Number.isFinite(message.graceSeconds) ? message.graceSeconds : 600;
        minutesSession = { sessionId: message.sessionId, roomId, captureId: captureSessionId, mode: message.mode, state: 'active' };
        publishMinutes(minutesSession, captureUserId);
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

async function start(streamId: string, sessionId: string, room?: string, userId?: string, mode: MomMode = 'live_only') {
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
    void selection.then((stream) => stream.getTracks().forEach((track) => track.stop()), () => undefined);
    throw new DOMException(problem('MI-CAP-006'), 'AbortError');
  }
  stopping = false;
  captureSessionId = sessionId;
  roomId = room;
  captureUserId = userId;
  captureMode = mode;
  minutesSession = undefined;
  const selectedTab = await selection.catch((error) => {
    assertCurrent(generation);
    throw error;
  });
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
    assertCurrent(generation);
    if (error instanceof Error && (error.name === 'NotAllowedError' || error.name === 'SecurityError')) {
      const permission = await navigator.permissions?.query({ name: 'microphone' as PermissionName }).catch(() => undefined);
      assertCurrent(generation);
      // Passive "prompt" and unsupported permission queries cannot establish
      // that a previous successful grant was revoked. Only explicit denial can.
      if (permission?.state === 'denied') {
        await chrome.runtime.sendMessage({ type: 'MICROPHONE_PERMISSION_STATUS', granted: false }).catch(() => undefined);
        assertCurrent(generation);
        throw new Error(problem('MI-MIC-001'));
      }
      throw new Error(problem(permission?.state === 'granted' ? 'MI-MIC-002' : 'MI-MIC-001'));
    }
    throw error;
  }


  notify({ captureStatus: 'connecting', backendConnected: false, timeline: undefined, error: undefined });
  await connectSocket(generation);

  audioContext = new AudioContext({ sampleRate: 16000 });
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
  let microphonePausedSince: number | undefined;
  let nextMicrophoneRecoveryAt = 0;
  let microphonePermissionBlocked = false;
  const recoverMicrophone = async () => {
    const now = Date.now();
    if (
      recoveringMicrophone
      || stopping
      || generation !== captureGeneration
      || now < nextMicrophoneRecoveryAt
      || microphonePermissionBlocked
    ) return;
    recoveringMicrophone = true;
    notify({ audioWarning: problem('MI-MIC-004') });
    try {
      // A live muted track may resume by itself after a device transition.
      // Keep it connected until a replacement is ready, so a failed reopen
      // cannot turn a temporary pause into a permanently disconnected input.
      const previousStream = microphoneStream;
      const previousSource = microphoneSource;
      if (!previousStream?.active || previousStream.getAudioTracks().every((track) => track.readyState === 'ended')) {
        previousSource?.disconnect();
        previousStream?.getTracks().forEach((track) => track.stop());
        microphoneSource = undefined;
        microphoneStream = undefined;
      }
      const replacement = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false,
      });
      if (stopping || generation !== captureGeneration) {
        replacement.getTracks().forEach((track) => track.stop());
        return;
      }
      let replacementSource: MediaStreamAudioSourceNode | undefined;
      try {
        replacementSource = sessionContext.createMediaStreamSource(replacement);
        replacementSource.connect(microphoneGain!);
        replacementSource.connect(microphoneAnalyser);
      } catch (error) {
        replacementSource?.disconnect();
        replacement.getTracks().forEach((track) => track.stop());
        throw error;
      }
      previousSource?.disconnect();
      previousStream?.getTracks().forEach((track) => track.stop());
      microphoneStream = replacement;
      microphoneSource = replacementSource;
      quietSince = Date.now();
      microphoneRecoveryAttempts = 0;
      microphonePausedSince = undefined;
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
        permissionState = await navigator.permissions?.query({ name: 'microphone' as PermissionName })
          .then((permission) => permission.state)
          .catch(() => undefined);
      }
      if (stopping || generation !== captureGeneration) return;
      // A transient NotAllowedError from an automatic background reopen is
      // not proof that the user revoked a previously granted permission.
      if (permissionState === 'denied') {
        microphonePermissionBlocked = true;
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
  retryMicrophoneCapture = () => {
    if (stopping || generation !== captureGeneration) return;
    microphonePermissionBlocked = false;
    nextMicrophoneRecoveryAt = 0;
    const tracks = microphoneStream?.getAudioTracks() ?? [];
    if (!microphoneStream?.active || tracks.length === 0 || tracks.every((track) => track.readyState === 'ended' || track.muted)) {
      void recoverMicrophone();
    }
  };
  // Check actual inputs: capture can still run while its sources are silent.
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
      microphonePausedSince = undefined;
      void recoverMicrophone();
      return;
    }
    const microphonePaused = microphoneTracks.every((track) => track.muted || !track.enabled);
    if (microphonePaused) {
      microphonePausedSince ??= Date.now();
      // Avoid reopening healthy tracks during short system/meeting pauses, or
      // overriding a disabled track. A sustained browser mute can be recovered
      // without releasing the original input before the replacement succeeds.
      if (Date.now() - microphonePausedSince >= 30_000 && microphoneTracks.some((track) => track.enabled)) {
        void recoverMicrophone();
      } else notify({ audioWarning: problem('MI-MIC-005') });
      return;
    }
    microphonePausedSince = undefined;
    if (recoveringMicrophone) return;
    const hasAudio = level(microphoneAnalyser) > 0.001 || level(tabAnalyser) > 0.001;
    if (hasAudio) quietSince = Date.now();
    notify({ audioWarning: Date.now() - quietSince > 15000
      ? problem('MI-MIC-008')
      : undefined });
  }, 3000);

  pumpTimer = setInterval(flushAudio, 100);
  const enqueue = (data: Blob) => {
    if (generation !== captureGeneration || stopping || !data.size) return;
    try { replay.append(data); }
    catch {
      notify({ captureStatus: 'error', backendConnected: false, error: problem('MI-CON-005') });
      void stop();
      return;
    }
    flushAudio();
  };
  const capture = await PcmCapture.start(sessionContext, recordingStream, enqueue, () => {
    if (generation !== captureGeneration || stopping) return;
    notify({ captureStatus: 'error', backendConnected: false, error: problem('MI-STT-001') });
    void stop();
  });
  if (generation !== captureGeneration || stopping) {
    await capture.finish(() => {});
    throw new DOMException(problem('MI-CAP-006'), 'AbortError');
  }
  pcmCapture = capture;
}

function publishMinutes(meeting: MomMeeting, userId: string | undefined) {
  if (userId) chrome.runtime.sendMessage({ type: 'MOM_UPDATE', userId, meeting }).catch(() => undefined);
}

// Drain owns retired references only. Hardware stops before network finalization.
function drainMinutes(activeSocket: WebSocket | undefined,
  queue: PcmFrame[], sequence: number, sent: number, authorized: boolean,
  finishMinutes: boolean): Promise<boolean> {
  if (!activeSocket || activeSocket.readyState !== WebSocket.OPEN || !authorized) return Promise.resolve(false);
  return new Promise(resolve => {
    let ended = false, controlSent = false;
    let pumpTimer: ReturnType<typeof setInterval> | undefined;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    const complete = (ok: boolean) => {
      if (ended) return;
      ended = true;
      if (pumpTimer) clearInterval(pumpTimer);
      if (deadline) clearTimeout(deadline);
      activeSocket.onmessage = null;
      activeSocket.onclose = null;
      activeSocket.onerror = null;
      resolve(ok);
    };
    const pump = () => {
      if (ended) return;
      try {
        if (activeSocket.readyState !== WebSocket.OPEN) { complete(false); return; }
        const frame = queue.find(frame => frame.end > sent);
        if (frame) {
          if ((activeSocket.bufferedAmount || 0) > 64_000) return;
          const start = Math.max(frame.start, sent);
          activeSocket.send(JSON.stringify({ type: 'audio', startSample: start }));
          activeSocket.send(frame.data.slice((start-frame.start)*2));
          sent = frame.end;
        }
        if (!controlSent && sent === sequence) {
          controlSent = true;
          activeSocket.send(JSON.stringify({ type: finishMinutes ? 'finish' : 'end', lastSequence: sequence }));
        }
      } catch { complete(false); }
    };
    activeSocket.onmessage = event => {
      if (typeof event.data !== 'string') return;
      try {
        const message = JSON.parse(event.data);
        if (controlSent && message.type === 'meeting_segment_ended' && message.ack === sequence
          && message.minutesFinalizing === finishMinutes) complete(true);
      } catch { /* Only an exact durable acknowledgment completes the drain. */ }
    };
    activeSocket.onclose = activeSocket.onerror = () => complete(false);
    // 90 seconds of retained audio at 2x plus recognition drain and setup.
    deadline = setTimeout(() => complete(false), 70_000);
    pumpTimer = setInterval(pump, 100);
    pump();
  });
}

async function stop(finishMinutes = false): Promise<{ warning?: string }> {
  const retiringMinutes = minutesSession;
  const retiringUser = captureUserId;
  const retiringGrace = minutesGrace;
  const needsMinutes = captureMode !== 'live_only' || !!retiringMinutes;
  finishMinutes = finishMinutes && !!minutesSession;
  const wasAuthorized = socketAuthorized;
  const pendingAudio = replay.frames.slice();
  let finalSequence = replay.end;
  const lastSent = sentThrough;
  const activePcm = pcmCapture;
  pcmCapture = undefined;
  const pcmFinished = activePcm?.finish(data => {
    if (data.size) {
      const start = finalSequence;
      finalSequence += data.size / 2;
      pendingAudio.push({ start, end: finalSequence, data });
    }
  });
  // Attach a rejection handler immediately; hardware cleanup happens before awaiting it.
  void pcmFinished?.catch(() => undefined);
  minutesSession = undefined;
  captureMode = 'live_only';
  captureGeneration += 1;
  stopping = true;
  socketAuthorized = false;
  authorizedUntil = 0;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  reconnectTimer = heartbeatTimer = undefined;
  reconnectStartedAt = reconnectAttempt = 0;
  sentThrough = nextSendAt = recoveryCount = 0;
  replay = new PcmReplay();
  if (pumpTimer) clearInterval(pumpTimer);
  pumpTimer = undefined;
  if (authTimer) clearTimeout(authTimer);
  authTimer = undefined;
  if (audioHealthTimer) clearInterval(audioHealthTimer);
  audioHealthTimer = undefined;
  const activeRecordingStream = recordingStream;
  const activeTabStream = tabStream;
  const activeMicrophoneStream = microphoneStream;
  const activeAudioContext = audioContext;
  const activeSocket = socket;
  const activeTabSource = tabSource;
  const activeMicrophoneSource = microphoneSource;
  const activeTabGain = tabGain;
  const activeMicrophoneGain = microphoneGain;
  // Detach everything before the first await. A second stop or later start
  // must never inherit these references or have them erased by late cleanup.
  recordingStream = tabStream = microphoneStream = undefined;
  audioContext = undefined;
  socket = undefined;
  tabSource = microphoneSource = undefined;
  tabGain = microphoneGain = undefined;
  retryMicrophoneCapture = undefined;

  const drained = activePcm ? (async () => {
    try { await pcmFinished; } catch { return false; }
    return drainMinutes(activeSocket, pendingAudio, finalSequence, lastSent, wasAuthorized, finishMinutes);
  })() : undefined;

  // Release the selected tab and mic before waiting for any network work.
  activeRecordingStream?.getTracks().forEach((track) => track.stop());
  activeTabStream?.getTracks().forEach((track) => track.stop());
  activeMicrophoneStream?.getTracks().forEach((track) => track.stop());
  if (!drained) activeSocket?.close();
  activeTabSource?.disconnect();
  activeMicrophoneSource?.disconnect();
  activeTabGain?.disconnect();
  activeMicrophoneGain?.disconnect();

  // Close the mixer independently of network processing; hardware is already off.
  // Flush the worklet's final sub-frame while the mixer can still render.
  if (pcmFinished) await pcmFinished.catch(() => undefined);
  try { await activeAudioContext?.close(); } catch { /* Context may already be closed. */ }
  const complete = drained ? await drained : !needsMinutes;
  activeSocket?.close();
  if (retiringMinutes) publishMinutes({ ...retiringMinutes, state: complete && finishMinutes ? 'ready' : 'active',
    finalizeAfter: Math.floor(Date.now() / 1000) + retiringGrace, supportCode: complete ? undefined : 'MI-MOM-003' }, retiringUser);
  return complete ? {} : { warning: problem(retiringMinutes ? 'MI-MOM-003' : 'MI-STT-002') };
}

function notify(state: Record<string, unknown>) {
  chrome.runtime.sendMessage({ type: 'CAPTURE_STATE', captureSessionId, state }).catch(() => undefined);
}

chrome.runtime.onMessage.addListener((message: { type: string; streamId?: string; captureSessionId?: string; granted?: boolean; roomId?: string; userId?: string; mode?: MomMode; finishMinutes?: boolean }, _sender, sendResponse) => {
  if (message.type === 'MIC_PERMISSION_RESULT' && message.granted
    && _sender.url === chrome.runtime.getURL('mic-permission.html')) {
    retryMicrophoneCapture?.();
    return false;
  }
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
    start(message.streamId, message.captureSessionId, message.roomId, message.userId, message.mode)
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
    stop(message.finishMinutes).then(result => sendResponse({ ok: true, ...result }));
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
