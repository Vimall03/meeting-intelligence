import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function harness({ failReplacementName, permissionState = 'prompt' } = {}) {
  const messages = [], timeouts = new Map(), intervals = new Map(), sockets = [], recorders = [], contexts = [];
  let listener, timer = 0;
  const track = () => ({ readyState: 'live', muted: false, enabled: true, stop() { this.readyState = 'ended'; } });
  const stream = () => { const audio = track(); return { active: true, getTracks: () => [audio], getAudioTracks: () => [audio], getVideoTracks: () => [] }; };
  const tab = stream(), microphone = stream(), replacement = stream();
  let micRequests = 0;
  class Socket {
    static OPEN = 1;
    readyState = 1;
    sent = [];
    constructor() { sockets.push(this); }
    send(value) { this.sent.push(value); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  class Recorder {
    static isTypeSupported() { return true; }
    state = 'inactive';
    listeners = {};
    constructor() { recorders.push(this); }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.ondataavailable?.({ data: { size: 50 } }); this.onstop?.(); this.listeners.stop?.(); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
  }
  const node = () => ({ connect() {}, disconnect() {}, gain: { value: 1 }, getFloatTimeDomainData(data) { data.fill(0.1); } });
  class Context {
    state = 'running';
    destination = {};
    resumed = 0;
    constructor() { contexts.push(this); }
    async resume() { this.resumed++; this.state = 'running'; }
    async close() { this.state = 'closed'; }
    createMediaStreamSource() { return node(); }
    createMediaStreamDestination() { return { ...node(), stream: stream() }; }
    createGain() { return node(); }
    createAnalyser() { return node(); }
  }
  const source = fs.readFileSync(new URL('../entrypoints/offscreen.ts', import.meta.url), 'utf8');
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, {
    exports: {}, require(name) { return name.includes('config') ? { AUDIO_MIME_TYPE: 'audio/webm', AUDIO_WINDOW_MS: 3000, MEETING_SOCKET_URL: 'ws://localhost' } : { isTimelineSnapshot: message => message.type === 'timeline_snapshot' }; },
    defineUnlistedScript: callback => callback(), DOMException, Error, MediaRecorder: Recorder, WebSocket: Socket, AudioContext: Context,
    navigator: { mediaDevices: { async getUserMedia(options) {
      if (options.audio.mandatory) return tab;
      if (++micRequests === 1) return microphone;
      if (failReplacementName) throw new DOMException('Microphone reopen failed', failReplacementName);
      return replacement;
    } }, permissions: { async query() { return { state: permissionState }; } } },
    chrome: { runtime: { onMessage: { addListener(callback) { listener = callback; } }, async sendMessage(message) { messages.push(message); } } },
    setTimeout(callback) { timeouts.set(++timer, callback); return timer; }, clearTimeout(id) { timeouts.delete(id); },
    setInterval(callback) { intervals.set(++timer, callback); return timer; }, clearInterval(id) { intervals.delete(id); },
  });
  return { messages, sockets, recorders, contexts, microphone, replacement, intervals,
    send: message => new Promise(resolve => listener(message, {}, resolve)),
    window() { const [id, callback] = timeouts.entries().next().value; timeouts.delete(id); callback(); },
    health() { for (const callback of intervals.values()) callback(); },
  };
}

test('capture keeps sending audio windows after a suggestion for more than a minute', async () => {
  const h = harness();
  assert.equal((await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' })).ok, true);
  const socket = h.sockets[0];
  socket.onmessage({ data: JSON.stringify({ type: 'timeline_snapshot', items: [{ kind: 'task_suggestion' }] }) });
  for (let i = 0; i < 25; i++) h.window();
  assert.equal(socket.sent.length, 25);
  assert.equal(h.recorders[0].state, 'recording');
  await h.send({ type: 'STOP_OFFSCREEN' });
  assert.equal(h.intervals.size, 0);
});

test('capture resumes a suspended mixer and replaces a disconnected microphone', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  h.contexts[0].state = 'suspended';
  h.health();
  assert.equal(h.contexts[0].state, 'running');
  h.microphone.getTracks()[0].readyState = 'ended';
  h.health();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.replacement.getTracks()[0].readyState, 'live');
  assert.equal(h.recorders[0].state, 'recording');
  h.window();
  assert.equal(h.sockets[0].sent.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
  assert.equal(h.replacement.getTracks()[0].readyState, 'ended');
});

test('capture replaces a microphone that becomes muted while its track remains live', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  const staleTrack = h.microphone.getTracks()[0];
  staleTrack.muted = true;

  h.health();
  h.health();
  assert.equal(staleTrack.readyState, 'live');
  assert.match(h.messages.at(-1).state.audioWarning, /paused briefly/);
  h.health();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(staleTrack.readyState, 'ended');
  assert.equal(h.replacement.getTracks()[0].readyState, 'live');
  assert.equal(h.messages.at(-1).state.audioWarning, undefined);
  h.window();
  assert.equal(h.sockets[0].sent.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('failed microphone recovery keeps the websocket and Meet capture alive', async () => {
  const h = harness({ failReplacementName: 'NotReadableError' });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  const socket = h.sockets[0];
  h.microphone.getTracks()[0].readyState = 'ended';
  h.health();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(socket.readyState, 1);
  assert.equal(h.recorders[0].state, 'recording');
  assert.equal(h.messages.at(-1).state.captureStatus, undefined);
  assert.match(h.messages.at(-1).state.audioWarning, /reconnect is pending/);

  h.window();
  assert.equal(socket.sent.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('a transient automatic NotAllowedError does not revoke remembered microphone access', async () => {
  const h = harness({ failReplacementName: 'NotAllowedError', permissionState: 'prompt' });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  h.microphone.getTracks()[0].readyState = 'ended';
  h.health();
  await new Promise(resolve => setImmediate(resolve));

  const permissionUpdates = h.messages.filter(message => message.type === 'MICROPHONE_PERMISSION_STATUS');
  assert.deepEqual(permissionUpdates.map(message => message.granted), [true]);
  assert.match(h.messages.at(-1).state.audioWarning, /reconnect is pending/);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('an explicit denied permission state clears remembered microphone access', async () => {
  const h = harness({ failReplacementName: 'NotAllowedError', permissionState: 'denied' });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  h.microphone.getTracks()[0].readyState = 'ended';
  h.health();
  await new Promise(resolve => setImmediate(resolve));

  const permissionUpdates = h.messages.filter(message => message.type === 'MICROPHONE_PERMISSION_STATUS');
  assert.deepEqual(permissionUpdates.map(message => message.granted), [true, false]);
  assert.match(h.messages.at(-1).state.audioWarning, /blocked/);
  await h.send({ type: 'STOP_OFFSCREEN' });
});
