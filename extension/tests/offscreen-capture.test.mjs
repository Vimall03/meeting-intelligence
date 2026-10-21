import * as userMessages from '../lib/user-messages.ts';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const replayExports = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(new URL('../lib/pcm-replay.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, { exports: replayExports, require: () => userMessages });

function harness({ failInitialName, failReplacementName, permissionState = 'prompt', deferReplacement = false, deferFirstClose = false, acknowledge = true, momMode, autoEnd = true, deferPcmFinish = false, clock = { now: Date.now() } } = {}) {
  const messages = [], timeouts = new Map(), intervals = new Map(), sockets = [], contexts = [];
  const pcmCaptures = [];
  let listener, timer = 0, serverAck = 0, lastMeeting;
  class Clock extends Date { static now() { return clock.now; } }
  const track = () => ({ readyState: 'live', muted: false, enabled: true, stop() { this.readyState = 'ended'; } });
  const stream = () => { const audio = track(); return { active: true, getTracks: () => [audio], getAudioTracks: () => [audio], getVideoTracks: () => [] }; };
  const tab = stream(), microphone = stream(), replacement = stream();
  let micRequests = 0, tabRequests = 0, resolveReplacement, resolveFirstClose;
  const pendingReplacement = new Promise(resolve => { resolveReplacement = resolve; });
  const pendingFirstClose = new Promise(resolve => { resolveFirstClose = resolve; });
  const acquiredMicrophones = [], acquiredTabs = [];
  class Socket {
    static OPEN = 1;
    readyState = 1;
    sent = [];
    constructor() { sockets.push(this); }
    controls = [];
    send(value) {
      if (typeof value === 'string') {
        const control = JSON.parse(value); this.controls.push(control);
        if (control.type === 'authenticate') queueMicrotask(() => {
          if(control.meetingId && control.meetingId !== lastMeeting) {serverAck=0;lastMeeting=control.meetingId;}
          this.onmessage?.({ data: JSON.stringify({ type: 'auth_ready', ack: serverAck, expiresAt: Math.floor(Date.now() / 1000) + 900, expiresIn: 900,
            audioFormat:'pcm_s16le_16000_v2' }) });
          if (momMode) this.onmessage?.({ data: JSON.stringify({ type: 'mom_session', sessionId: 'a'.repeat(32), mode: momMode, graceSeconds: 600 }) });
        });
        if (autoEnd && ['end', 'finish'].includes(control.type)) queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: 'meeting_segment_ended', ack: control.lastSequence, minutesFinalizing: control.type === 'finish' }) }));
        if (control.type === 'audio') this.startSample = control.startSample;
        if (control.type === 'ping') this.onmessage?.({ data: JSON.stringify({ type: 'pong' }) });
      } else {
        this.sent.push(value);
        if (acknowledge) { serverAck = this.startSample + value.size / 2; const ack=serverAck; queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: 'audio_ack', sequence: ack }) })); }
      }
    }
    close() { this.readyState = 3; this.onclose?.({ code: 1000 }); }
  }
  const node = () => ({ connect() {}, disconnect() {}, gain: { value: 1 }, getFloatTimeDomainData(data) { data.fill(0.1); } });
  class Context {
    state = 'running';
    destination = {};
    resumed = 0;
    constructor() { contexts.push(this); }
    async resume() { this.resumed++; this.state = 'running'; }
    async close() { if (deferFirstClose && this === contexts[0]) await pendingFirstClose; this.state = 'closed'; }
    createMediaStreamSource() { return node(); }
    createMediaStreamDestination() { return { ...node(), stream: stream() }; }
    createGain() { return node(); }
    createAnalyser() { return node(); }
  }
  const source = fs.readFileSync(new URL('../entrypoints/offscreen.ts', import.meta.url), 'utf8');
  class Pcm {
    static async start(context, stream, sink, onError) { const value=new Pcm();value.sink=sink;value.onError=onError;pcmCaptures.push(value);return value; }
    emit(size=6400) { this.sink(new Blob([new Uint8Array(size)])); }
    finish(sink) { this.sink=sink;this.emit(100);return deferPcmFinish ? new Promise(resolve=>{this.resolve=resolve;}) : Promise.resolve(); }
  }
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, {
    exports: {}, require(name) { if (name.includes('user-messages')) return userMessages; if(name.includes('pcm-replay'))return replayExports; if(name.includes('pcm-capture'))return {PcmCapture:Pcm}; return name.includes('config') ? { MEETING_SOCKET_URL: 'ws://localhost' } : { isTimelineSnapshot: message => message.type === 'timeline_snapshot' }; },
    defineUnlistedScript: callback => callback(), queueMicrotask, Date: Clock, DOMException, Error, WebSocket: Socket, AudioContext: Context,
    navigator: { mediaDevices: { async getUserMedia(options) {
      if (options.audio.mandatory) { const selected = ++tabRequests === 1 ? tab : stream(); acquiredTabs.push(selected); return selected; }
      if (++micRequests === 1) {
        if (failInitialName) throw new DOMException('Microphone open failed', failInitialName);
        acquiredMicrophones.push(microphone); return microphone;
      }
      if (deferReplacement && micRequests === 2) { const selected = await pendingReplacement; acquiredMicrophones.push(selected); return selected; }
      if (failReplacementName && micRequests === 2) throw new DOMException('Microphone reopen failed', failReplacementName);
      const selected = micRequests === 2 ? replacement : stream(); acquiredMicrophones.push(selected); return selected;
    } }, permissions: { async query() { return { state: permissionState }; } } },
    chrome: { runtime: { getURL: path => `chrome-extension://test/${path}`, onMessage: { addListener(callback) { listener = callback; } }, async sendMessage(message) { messages.push(message); if (message.type === 'MEETING_TICKET') return { ok: true, ticket: 'ticket' }; } } },
    setTimeout(callback, delay) { timeouts.set(++timer, { callback, delay }); return timer; }, clearTimeout(id) { timeouts.delete(id); },
    setInterval(callback, delay) { intervals.set(++timer, { callback, delay }); return timer; }, clearInterval(id) { intervals.delete(id); },
  });
  return { messages, sockets, contexts, microphone, replacement, intervals, acquiredMicrophones, acquiredTabs, pcmCaptures,
    get micRequests() { return micRequests; },
    resolveReplacement: () => resolveReplacement(replacement),
    resolveFirstClose: () => resolveFirstClose(),
    grantMicrophone(url = 'chrome-extension://test/mic-permission.html') { listener({ type: 'MIC_PERMISSION_RESULT', granted: true }, { tab: { id: 2 }, url }, () => {}); },
    send: async message => {
      let finished=false;
      const response=new Promise(resolve => listener(message, {}, value=>{finished=true;resolve(value);}));
      if (message.type === 'STOP_OFFSCREEN' && autoEnd) {
        for(let i=0;i<1000&&!finished;i++) {
          await new Promise(resolve=>setImmediate(resolve)); clock.now+=100;
          for(const task of [...intervals.values()])if(task.delay===100)task.callback();
        }
      }
      const result=await response;
      if(message.type==='START_OFFSCREEN'&&result.ok) { sockets.at(-1).onopen(); await new Promise(resolve=>setImmediate(resolve)); }
      return result;
    },
    window() { clock.now+=200; pcmCaptures.at(-1).emit(); },
    pump() {clock.now+=100; for(const task of [...intervals.values()])if(task.delay===100)task.callback();},
    renew() { const [id, { callback }] = [...timeouts.entries()].find(([, task]) => task.delay > 10000); timeouts.delete(id); callback(); },
    health() { for (const {callback, delay} of intervals.values()) if (delay !== 15000) callback(); },
    heartbeat() { for (const {callback, delay} of intervals.values()) if (delay === 15000) callback(); },
    async reconnect() { const [id, {callback}] = [...timeouts.entries()].find(([, task]) => task.delay >= 1000 && task.delay < 2000); timeouts.delete(id); callback(); await new Promise(resolve => setImmediate(resolve)); sockets.at(-1).onopen(); await new Promise(resolve => setImmediate(resolve)); clock.now+=100;for(const task of [...intervals.values()])if(task.delay===100)task.callback(); },
    timeout(delay) { const [id, task] = [...timeouts.entries()].find(([, task]) => task.delay === delay); timeouts.delete(id); task.callback(); },
    tab,
    clock,
    timeouts,
  };
}

test('capture keeps sending audio frames after a suggestion for more than a minute', async () => {
  const h = harness();
  assert.equal((await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' })).ok, true);
  const socket = h.sockets[0];
  socket.onmessage({ data: JSON.stringify({ type: 'timeline_snapshot', items: [{ kind: 'task_suggestion' }] }) });
  for (let i = 0; i < 25; i++) h.window();
  assert.equal(socket.sent.length, 25);
  assert.equal(h.pcmCaptures.length, 1);
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
  assert.equal(h.pcmCaptures.length, 1);
  h.window();
  assert.equal(h.sockets[0].sent.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
  assert.equal(h.replacement.getTracks()[0].readyState, 'ended');
});

test('sustained mute replaces microphone after a grace period while a short pause preserves the input', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  const staleTrack = h.microphone.getTracks()[0];
  staleTrack.muted = true;

  h.health();
  h.health();
  assert.equal(staleTrack.readyState, 'live');
  assert.match(h.messages.at(-1).state.audioWarning, /briefly paused/);
  h.clock.now += 30_000;
  h.health();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(staleTrack.readyState, 'ended');
  assert.equal(h.replacement.getTracks()[0].readyState, 'live');
  assert.equal(h.messages.at(-1).state.audioWarning, undefined);
  h.window();
  assert.equal(h.sockets[0].sent.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('a disabled live microphone is preserved instead of repeatedly reopened', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  const track = h.microphone.getTracks()[0];
  track.enabled = false;
  for (let i = 0; i < 30; i++) { h.clock.now += 3000; h.health(); }
  assert.equal(h.micRequests, 1);
  assert.equal(track.readyState, 'live');
  track.enabled = true;
  h.health();
  assert.equal(h.messages.at(-1).state.audioWarning, undefined);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('failed muted-track replacement keeps the original input connected so it can resume', async () => {
  const h = harness({ failReplacementName: 'NotAllowedError' });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  const track = h.microphone.getTracks()[0];
  track.muted = true;
  h.health(); h.clock.now += 30_000; h.health();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(track.readyState, 'live');
  track.muted = false;
  h.health();
  assert.equal(h.messages.at(-1).state.audioWarning, undefined);
  assert.equal(h.pcmCaptures.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('an inconclusive automatic startup error does not erase a remembered grant', async () => {
  const h = harness({ failInitialName: 'NotAllowedError' });
  const result = await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  assert.equal(result.ok, false);
  assert.match(result.error, /MI-MIC-001/);
  assert.equal(h.messages.some(message => message.type === 'MICROPHONE_PERMISSION_STATUS' && message.granted === false), false);
  assert.equal(h.tab.getTracks()[0].readyState, 'ended');
});

test('failed microphone recovery keeps the websocket and Meet capture alive', async () => {
  const h = harness({ failReplacementName: 'NotReadableError' });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  const socket = h.sockets[0];
  h.microphone.getTracks()[0].readyState = 'ended';
  h.health();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(socket.readyState, 1);
  assert.equal(h.pcmCaptures.length, 1);
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

test('explicit denial waits for a trusted fresh grant and recovers without replacing capture resources', async () => {
  const h = harness({ failReplacementName: 'NotAllowedError', permissionState: 'denied' });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  const socket = h.sockets[0], encoder = h.pcmCaptures[0], context = h.contexts[0];
  h.microphone.getTracks()[0].readyState = 'ended';
  h.health();
  await new Promise(resolve => setImmediate(resolve));

  const permissionUpdates = h.messages.filter(message => message.type === 'MICROPHONE_PERMISSION_STATUS');
  assert.deepEqual(permissionUpdates.map(message => message.granted), [true, false]);
  assert.match(h.messages.at(-1).state.audioWarning, /blocked/);
  for (let i = 0; i < 10; i++) { h.clock.now += 30_000; h.health(); }
  assert.equal(h.micRequests, 2);
  h.grantMicrophone('https://meet.google.com/abc-defg-hij');
  h.grantMicrophone('chrome-extension://test/sidepanel.html');
  h.grantMicrophone('chrome-extension://another-extension/mic-permission.html');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.micRequests, 2);
  h.grantMicrophone();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.micRequests, 3);
  assert.equal(h.messages.at(-1).state.audioWarning, undefined);
  assert.deepEqual(h.sockets, [socket]);
  assert.deepEqual(h.pcmCaptures, [encoder]);
  assert.deepEqual(h.contexts, [context]);
  assert.equal(h.acquiredMicrophones.at(-1).getTracks()[0].readyState, 'live');
  h.window();
  assert.equal(socket.sent.length, 1);
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

test('late cleanup from the previous capture cannot erase a restarted microphone or socket', async () => {
  const h = harness({ deferFirstClose: true });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'first' });
  const stopping = h.send({ type: 'STOP_OFFSCREEN' });
  await new Promise(resolve => setImmediate(resolve));
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'second' });
  const activeMicrophone = h.acquiredMicrophones.at(-1);
  const activeSocket = h.sockets.at(-1);
  h.resolveFirstClose();
  await stopping;
  assert.equal(activeMicrophone.getTracks()[0].readyState, 'live');
  assert.equal(activeSocket.readyState, 1);
  assert.equal((await h.send({ type: 'GET_OFFSCREEN_STATUS' })).active, true);
  h.window();
  assert.equal(activeSocket.sent.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
  assert.equal(activeMicrophone.getTracks()[0].readyState, 'ended');
});

test('repeated capture sessions release each input while preserving the next session', async () => {
  const h = harness();
  for (let i = 0; i < 8; i++) {
    assert.equal((await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: `session-${i}` })).ok, true);
    assert.equal(h.acquiredMicrophones.at(-1).getTracks()[0].readyState, 'live');
    h.window();
    assert.equal(h.sockets.at(-1).sent.length, 1);
    await h.send({ type: 'STOP_OFFSCREEN' });
    assert.equal(h.acquiredMicrophones.at(-1).getTracks()[0].readyState, 'ended');
    assert.equal(h.acquiredTabs.at(-1).getTracks()[0].readyState, 'ended');
    assert.equal(h.intervals.size, 0);
    assert.equal(h.timeouts.size, 0);
  }
  assert.equal(h.micRequests, 8);
});


test('authorization renewal preserves the same microphone, mixer, encoder and socket', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  const socket = h.sockets[0], mixer = h.contexts[0], encoder = h.pcmCaptures[0];
  h.window();
  h.renew();
  await new Promise(resolve => setImmediate(resolve));
  h.window();
  assert.equal(h.sockets.length, 1);
  assert.equal(h.contexts.length, 1);
  assert.equal(h.pcmCaptures.length, 1);
  assert.equal(h.sockets[0], socket);
  assert.equal(h.contexts[0], mixer);
  assert.equal(h.pcmCaptures[0], encoder);
  assert.equal(h.microphone.getTracks()[0].readyState, 'live');
  assert.equal(socket.sent.length, 2);
  await h.send({ type: 'STOP_OFFSCREEN' });
});


test('server-relative lifetime avoids renewal loops when the device clock differs', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'session' });
  h.sockets[0].onmessage({ data: JSON.stringify({ type: 'auth_ready', expiresAt: 1, expiresIn: 900, ack: 0, audioFormat: 'pcm_s16le_16000_v2' }) });
  h.renew(); await new Promise(resolve => setImmediate(resolve)); h.window();
  assert.equal(h.sockets[0].readyState, 1);
  assert.equal(h.microphone.getTracks()[0].readyState, 'live');
  assert.equal(h.sockets[0].sent.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('server restart reconnects without reopening mic or recreating encoder and replays unacknowledged audio', async () => {
  const h = harness({ acknowledge: false });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'meeting' });
  h.window();
  const original = h.sockets[0];
  original.onclose({ code: 1012 });
  h.window();
  assert.equal(h.microphone.getTracks()[0].readyState, 'live');
  assert.equal(h.tab.getTracks()[0].readyState, 'live');
  assert.equal(h.pcmCaptures.length, 1);
  assert.equal(h.contexts.length, 1);
  await h.reconnect();
  assert.equal(h.sockets.length, 2);
  assert.equal(h.sockets[1].controls[0].meetingId, 'meeting');
  assert.equal(h.sockets[1].sent.length, 2);
  h.window();
  assert.equal(h.sockets[1].sent.length, 3);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('acknowledged audio is not replayed and stale old socket cannot stop replacement', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'meeting' });
  h.window();
  const first = h.sockets[0];
  first.onclose({ code: 1006 });
  await h.reconnect();
  assert.equal(h.sockets[1].sent.length, 0);
  first.onclose({ code: 4401 });
  assert.equal(h.microphone.getTracks()[0].readyState, 'live');
  h.window();
  assert.equal(h.sockets[1].sent.length, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('stop during reconnect cancels retry and releases all capture resources', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'meeting' });
  h.sockets[0].onclose({ code: 1012 });
  await h.send({ type: 'STOP_OFFSCREEN' });
  assert.equal(h.timeouts.size, 0);
  assert.equal(h.intervals.size, 0);
  assert.equal(h.microphone.getTracks()[0].readyState, 'ended');
  assert.equal(h.tab.getTracks()[0].readyState, 'ended');
});

test('expired authentication stops capture instead of reconnecting', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'meeting' });
  h.sockets[0].onclose({ code: 4401 });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.microphone.getTracks()[0].readyState, 'ended');
  assert.equal(h.timeouts.size, 0);
});

test('audio queue stays bounded when server never acknowledges', async () => {
  const h = harness({ acknowledge: false });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'meeting' });
  for (let i = 0; i < 451; i++) h.window();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.microphone.getTracks()[0].readyState, 'ended');
  assert.match(h.messages.find(message => message.state?.error?.includes('MI-CON-005')).state.error, /unavailable for too long/);
});

test('application heartbeat sends ping without touching media', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'meeting' });
  h.heartbeat();
  assert.equal(h.sockets[0].controls.at(-1).type, 'ping');
  assert.equal(h.pcmCaptures.length, 1);
  assert.equal(h.contexts[0].state, 'running');
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('missing server heartbeat starts recovery without ending microphone capture', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'meeting' });
  h.clock.now += 36_000;
  h.heartbeat();
  assert.equal(h.microphone.getTracks()[0].readyState, 'live');
  assert.equal(h.tab.getTracks()[0].readyState, 'live');
  assert.equal(h.sockets[0].readyState, 3);
  assert.match(h.messages.at(-1).state.error, /MI-CON-004/);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('recovery deadline ends capture with a clear notice rather than retrying indefinitely', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'meeting' });
  h.sockets[0].onclose({ code: 1012 });
  h.clock.now += 61_000;
  // A new attempt fails too, after the recovery deadline.
  const [id, task] = [...h.timeouts.entries()].find(([, task]) => task.delay >= 1000 && task.delay < 2000);
  h.timeouts.delete(id); task.callback();
  await new Promise(resolve => setImmediate(resolve));
  h.sockets.at(-1).onclose({ code: 1013 });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.microphone.getTracks()[0].readyState, 'ended');
  assert.match(h.messages.find(message => message.state?.error?.includes('MI-CON-005')).state.error, /MI-CON-005/);
});


test('MOM sends room identity separately and drains the final blob before finish acknowledgment', async () => {
  const h = harness({ momMode: 'both' });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'capture', roomId: 'abc-defg-hij', userId: 'user', mode: 'both' });
  const socket = h.sockets[0];
  assert.equal(socket.controls[0].meetingId, 'capture');
  assert.equal(socket.controls[0].roomId, 'abc-defg-hij');
  h.window();
  const stopped = h.send({ type: 'STOP_OFFSCREEN', finishMinutes: true });
  assert.equal(h.microphone.getTracks()[0].readyState, 'ended');
  assert.equal(h.tab.getTracks()[0].readyState, 'ended');
  assert.equal((await stopped).warning, undefined);
  assert.equal(socket.sent.length, 2);
  assert.equal(socket.controls.at(-1).type, 'finish');
  assert.equal(socket.controls.at(-1).lastSequence, 3250);
  assert.equal(socket.readyState, 3);
  assert.equal(h.messages.filter(m => m.type === 'MOM_UPDATE').at(-1).meeting.state, 'ready');
  assert.equal(h.timeouts.size, 0);
  assert.equal(h.intervals.size, 0);
});

test('MOM ordinary stop starts rejoin grace and does not finish minutes', async () => {
  const h = harness({ momMode: 'mom_only' });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'capture', roomId: 'abc-defg-hij', userId: 'user', mode: 'mom_only' });
  await h.send({ type: 'STOP_OFFSCREEN' });
  assert.equal(h.sockets[0].controls.at(-1).type, 'end');
  assert.equal(h.messages.filter(m => m.type === 'MOM_UPDATE').at(-1).meeting.state, 'active');
  assert.equal(h.messages.some(m => m.type === 'MICROPHONE_PERMISSION_STATUS' && !m.granted), false);
});

test('MOM reconnect keeps microphone and capture ID while sending the server resume ID', async () => {
  const h = harness({ momMode: 'both', acknowledge: false });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'capture', roomId: 'abc-defg-hij', userId: 'user', mode: 'both' });
  h.window();
  h.sockets[0].onclose({ code: 1012 });
  await h.reconnect();
  assert.equal(h.sockets[1].controls[0].momSessionId, 'a'.repeat(32));
  assert.equal(h.sockets[1].controls[0].meetingId, 'capture');
  assert.equal(h.sockets[1].sent.length, 1);
  assert.equal(h.micRequests, 1);
  await h.send({ type: 'STOP_OFFSCREEN' });
});

test('MOM drain timeout releases hardware immediately and reports incomplete tail', async () => {
  const h = harness({ momMode: 'both', autoEnd: false });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'capture', roomId: 'abc-defg-hij', userId: 'user', mode: 'both' });
  const stopped = h.send({ type: 'STOP_OFFSCREEN', finishMinutes: true });
  assert.equal(h.microphone.getTracks()[0].readyState, 'ended');
  assert.equal(h.tab.getTracks()[0].readyState, 'ended');
  await new Promise(resolve => setImmediate(resolve));
  h.timeout(70000);
  assert.match((await stopped).warning, /MI-MOM-003/);
  assert.equal(h.sockets[0].readyState, 3);
  assert.equal(h.intervals.size, 0);
});

test('late MOM drain completion cannot close the microphone or socket of a new capture', async () => {
  const h = harness({ momMode: 'both', autoEnd: false });
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'first', roomId: 'abc-defg-hij', userId: 'user', mode: 'both' });
  const stopped = h.send({ type: 'STOP_OFFSCREEN' });
  await new Promise(resolve => setImmediate(resolve));
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'second', roomId: 'abc-defg-hij', userId: 'user', mode: 'both' });
  h.sockets[0].onmessage({ data: JSON.stringify({ type: 'meeting_segment_ended', ack: 50, minutesFinalizing: false }) });
  await stopped;
  assert.equal(h.acquiredMicrophones.at(-1).getTracks()[0].readyState, 'live');
  assert.equal(h.sockets[1].readyState, 1);
  const secondStop = h.send({ type: 'STOP_OFFSCREEN' });
  await new Promise(resolve => setImmediate(resolve));
  h.timeout(70000); await secondStop;
});

test('live-only stop never sends a MOM finalization or persists minutes metadata', async () => {
  const h = harness();
  await h.send({ type: 'START_OFFSCREEN', streamId: 'tab', captureSessionId: 'capture', roomId: 'abc-defg-hij', userId: 'user' });
  await h.send({ type: 'STOP_OFFSCREEN', finishMinutes: true });
  assert.equal(h.sockets[0].controls.at(-1).type, 'end');
  assert.equal(h.messages.some(m => m.type === 'MOM_UPDATE'), false);
});

test('streaming capture keeps one encoder and socket for continuous PCM and drains on stop', async()=>{
  const h=harness({streaming:true});
  await h.send({type:'START_OFFSCREEN',streamId:'tab',captureSessionId:'streaming-session'});
  assert.equal(h.pcmCaptures.length,1);
  assert.equal(h.sockets[0].controls[0].audioFormat,'pcm_s16le_16000_v2');
  for(let n=0;n<100;n++)h.window();
  assert.equal(h.sockets.length,1);
  assert.equal(h.sockets[0].sent.length,100);
  const result=await h.send({type:'STOP_OFFSCREEN'});
  assert.equal(result.ok,true);
  assert.equal(h.sockets[0].sent.length,101,'flush final sub-frame');
  assert.ok(h.sockets[0].controls.some(c=>c.type==='end'&&c.lastSequence===320050));
  assert.equal(h.microphone.getTracks()[0].readyState,'ended');
  assert.equal(h.intervals.size,0);
});

test('streaming stops hardware immediately while final PCM is being flushed',async()=>{
  const h=harness({streaming:true,deferPcmFinish:true});
  await h.send({type:'START_OFFSCREEN',streamId:'tab',captureSessionId:'streaming-session'});
  const stopping=h.send({type:'STOP_OFFSCREEN'});
  assert.equal(h.microphone.getTracks()[0].readyState,'ended');
  assert.equal(h.tab.getTracks()[0].readyState,'ended');
  h.pcmCaptures[0].emit(80);
  h.pcmCaptures[0].resolve();
  await stopping;
  assert.equal(h.sockets[0].sent.length,2);
  assert.equal(h.contexts[0].state,'closed');
});

test('repeated connection flaps stop within the paid recovery budget',async()=>{
  const h=harness();
  await h.send({type:'START_OFFSCREEN',streamId:'tab',captureSessionId:'streaming-session'});
  for(let n=0;n<5;n++) { h.sockets.at(-1).onclose({code:1006});await h.reconnect(); }
  h.sockets.at(-1).onclose({code:1006});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.microphone.getTracks()[0].readyState,'ended');
  assert.equal(h.tab.getTracks()[0].readyState,'ended');
  assert.ok(h.messages.some(m=>m.state?.error?.includes('MI-CON-005')));
  assert.equal(h.intervals.size,0);
});
