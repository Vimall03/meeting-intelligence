import * as userMessages from '../lib/user-messages.ts';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function harness({ failReplacementName, permissionState = 'prompt', deferReplacement = false } = {}) {
  const messages = [], timeouts = new Map(), intervals = new Map(), sockets = [], recorders = [], contexts = [];
  let listener, timer = 0;
  const track = () => ({ readyState: 'live', muted: false, enabled: true, stop() { this.readyState = 'ended'; } });
  const stream = () => { const audio = track(); return { active: true, getTracks: () => [audio], getAudioTracks: () => [audio], getVideoTracks: () => [] }; };
  const tab = stream(), microphone = stream(), replacement = stream();
  let micRequests = 0, resolveReplacement;
  const pendingReplacement = new Promise(resolve => { resolveReplacement = resolve; });
  class Socket {
    static OPEN = 1;
    readyState = 1;
    sent = [];
    constructor() { sockets.push(this); }
    send(value) { if (typeof value === 'string') { queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: 'auth_ready', expiresAt: Math.floor(Date.now() / 1000) + 900, expiresIn: 900 }) })); } else this.sent.push(value); }
    close() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
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
    exports: {}, require(name) { if (name.includes('user-messages')) return userMessages; return name.includes('config') ? { AUDIO_MIME_TYPE: 'audio/webm', AUDIO_WINDOW_MS: 3000, MEETING_SOCKET_URL: 'ws://localhost' } : { isTimelineSnapshot: message => message.type === 'timeline_snapshot' }; },
    defineUnlistedScript: callback => callback(), queueMicrotask, DOMException, Error, MediaRecorder: Recorder, WebSocket: Socket, AudioContext: Context,
    navigator: { mediaDevices: { async getUserMedia(options) {
      if (options.audio.mandatory) return tab;
      if (++micRequests === 1) return microphone;
      if (deferReplacement) return pendingReplacement;
      if (failReplacementName) throw new DOMException('Microphone reopen failed', failReplacementName);
      return replacement;
    } }, permissions: { async query() { return { state: permissionState }; } } },
    chrome: { runtime: { onMessage: { addListener(callback) { listener = callback; } }, async sendMessage(message) { messages.push(message); if (message.type === 'MEETING_TICKET') return { ok: true, ticket: 'ticket' }; } } },
    setTimeout(callback, delay) { timeouts.set(++timer, { callback, delay }); return timer; }, clearTimeout(id) { timeouts.delete(id); },
    setInterval(callback) { intervals.set(++timer, callback); return timer; }, clearInterval(id) { intervals.delete(id); },
  });
  return { messages, sockets, recorders, contexts, microphone, replacement, intervals,
    resolveReplacement: () => resolveReplacement(replacement),
    send: async message => { const result = await new Promise(resolve => listener(message, {}, resolve)); if (message.type === 'START_OFFSCREEN') { sockets.at(-1).onopen(); await new Promise(resolve => setImmediate(resolve)); } return result; },
    window() { const [id, { callback }] = [...timeouts.entries()].find(([, task]) => task.delay === 3000); timeouts.delete(id); callback(); },
    renew() { const [id, { callback }] = [...timeouts.entries()].find(([, task]) => task.delay > 10000); timeouts.delete(id); callback(); },
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
  assert.match(h.messages.at(-1).state.audioWarning, /briefly paused/);
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
  assert.match(h.messages.at(-1).state.audioWarning, /still reconnecting/);

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
  assert.match(h.messages.at(-1).state.audioWarning, /still reconnecting/);
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

test('a disconnected microphone still resumes a suspended mixer', async () => {
  const h = harness({ failReplacementName: 'NotReadableError' });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  h.contexts[0].state = 'suspended';
  h.microphone.getTracks()[0].readyState = 'ended';
  h.health();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.contexts[0].state, 'running');
  assert.equal(h.sockets[0].readyState, 1);
  h.window();
  assert.equal(h.sockets[0].sent.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('a brief mute resolves without reopening the microphone', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  const track = h.microphone.getTracks()[0];
  track.muted = true;
  h.health();
  h.health();
  track.muted = false;
  h.health();
  assert.equal(track.readyState, 'live');
  assert.equal(h.messages.at(-1).state.audioWarning, undefined);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('stopping while microphone recovery is pending releases the late replacement', async () => {
  const h = harness({ deferReplacement: true });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  h.microphone.getTracks()[0].readyState = 'ended';
  h.health();
  await h.send({ type: 'STOP_OFFSCREEN' });
  const messageCount = h.messages.length;
  h.resolveReplacement();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.replacement.getTracks()[0].readyState, 'ended');
  assert.equal(h.contexts[0].state, 'closed');
  assert.equal(h.sockets[0].readyState, 3);
  assert.equal(h.messages.length, messageCount);
});


test('authorization renewal preserves the same microphone, mixer, recorder and socket', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  const socket = h.sockets[0], mixer = h.contexts[0], recorder = h.recorders[0];
  h.window();
  h.renew();
  await new Promise(resolve => setImmediate(resolve));
  h.window();
  assert.equal(h.sockets.length, 1);
  assert.equal(h.contexts.length, 1);
  assert.equal(h.recorders.length, 1);
  assert.equal(h.sockets[0], socket);
  assert.equal(h.contexts[0], mixer);
  assert.equal(h.recorders[0], recorder);
  assert.equal(h.microphone.getTracks()[0].readyState, 'live');
  assert.equal(socket.sent.length, 2);
  await h.send({ type: 'STOP_OFFSCREEN' });
});


test('server-relative lifetime avoids renewal loops when the device clock differs', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  h.sockets[0].onmessage({ data: JSON.stringify({ type: 'auth_ready', expiresAt: 1, expiresIn: 900 }) });
  h.renew(); await new Promise(resolve => setImmediate(resolve)); h.window();
  assert.equal(h.sockets[0].readyState, 1);
  assert.equal(h.microphone.getTracks()[0].readyState, 'live');
  assert.equal(h.sockets[0].sent.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
});
