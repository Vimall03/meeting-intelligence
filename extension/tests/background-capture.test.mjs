import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import * as userMessages from '../lib/user-messages.ts';
import * as meetings from '../lib/meeting-session.ts';
import * as permissions from '../lib/microphone-permission.ts';
import * as mom from '../lib/mom.ts';
import * as recovery from '../lib/content-script-recovery.ts';

function harness({ deferAccess = false, invocationDenied = false, savedState, activeOffscreen = false, expireAccess = false, momMode = 'live_only', momFailure = false, userId = 'user', meetingUrl = 'https://meet.google.com/abc-defg-hij' } = {}) {
  const calls = [], requests = [], offscreenStarts = [], local = {}, session = savedState ? { state: savedState } : {};
  let listener, signedOut, releaseAccess, active = activeOffscreen, offscreenExists = activeOffscreen;
  let clock = 0, issuedAt = 0, streams = 0, uuid = 0;
  let meetingStatus = 'in-meeting';
  const accessGate = new Promise(resolve => { releaseAccess = resolve; });
  const events = () => {
    const listeners = [];
    return { addListener(callback) { listeners.push(callback); }, fire(...args) { for (const callback of listeners) callback(...args); } };
  };
  const chrome = {
    runtime: {
      id: 'extension', lastError: undefined,
      onMessage: { addListener(value) { listener = value; } },
      async getContexts() { return offscreenExists ? [{}] : []; },
      getManifest: () => ({ content_scripts: [] }),
      async sendMessage(message) {
        calls.push(message.type);
        if (message.type === 'GET_OFFSCREEN_STATUS') return { active };
        if (message.type === 'STOP_OFFSCREEN') { active = false; return { ok: true }; }
        if (message.type === 'START_OFFSCREEN') {
          offscreenStarts.push(message);
          // Model Chrome's single-use, short-lived IDs: setup after minting
          // must not be allowed to burn most of their lifetime.
          assert.ok(clock - issuedAt < 3000, 'tab stream ID expired before it was consumed');
          active = true;
          await send({ type: 'CAPTURE_STATE', captureSessionId: message.captureSessionId,
            state: { captureStatus: 'capturing', backendConnected: true } },
          { id: 'extension', url: 'chrome-extension://extension/offscreen-page.html' });
          return { ok: true };
        }
      },
    },
    storage: {
      local: { async get(key) { return { [key]: local[key] }; }, async set(value) { Object.assign(local, value); }, async remove(key) { delete local[key]; } },
      session: { async get() { return session; }, async set(value) { Object.assign(session, value); } },
    },
    tabs: {
      async get(id) { calls.push(`tab:${id}`); return { id, active: true, url: meetingUrl }; },
      async sendMessage(id) { calls.push(`probe:${id}`); clock += 1000; return { status: meetingStatus, meetingId: meetings.meetingIdFromUrl(meetingUrl) }; },
      onRemoved: events(), onUpdated: events(), async remove() {},
    },
    scripting: { async executeScript() {} },
    offscreen: { Reason: { USER_MEDIA: 'USER_MEDIA' }, async createDocument() { calls.push('offscreen:ready'); clock += 1000; offscreenExists = true; } },
    tabCapture: {
      getMediaStreamId({ targetTabId }, callback) {
        calls.push(`mint:${targetTabId}`); streams++; issuedAt = clock;
        if (invocationDenied) chrome.runtime.lastError = { message: 'Extension has not been invoked for the current page (see activeTab permission).' };
        callback(invocationDenied ? undefined : `stream-${streams}`);
        chrome.runtime.lastError = undefined;
      },
    },
    sidePanel: { async setPanelBehavior() {}, async open() {} }, action: { onClicked: events() },
  };
  const send = (message, sender = { id: 'extension', url: 'chrome-extension://extension/sidepanel.html' }) =>
    new Promise(resolve => listener(message, sender, resolve));
  const source = fs.readFileSync(new URL('../entrypoints/background.ts', import.meta.url), 'utf8');
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, {
    exports: {}, chrome, URL, Response, setTimeout, Error,
    crypto: { randomUUID: () => `capture-${++uuid}` },
    defineBackground: callback => callback(),
    require(name) {
      if (name.includes('user-messages')) return userMessages;
      if (name.includes('meeting-session')) return meetings;
      if (name.includes('microphone-permission')) return permissions;
      if (name.includes('content-script-recovery')) return recovery;
      if (name.endsWith('/mom')) return mom;
      if (name.includes('background-auth')) return {
        async backendFetch(_auth, url, method, body) {
          calls.push('mom:settings'); clock += 1000; requests.push({url,method,body});
          if (momFailure) return { status: 503, data: { detail: {support_code:'MI-MOM-001',message:'provider-secret'} } };
          return { status: 200, data: url.includes('/sessions/') ? {state:'sent'} : { ...mom.defaultMomSettings, mode: body ? JSON.parse(body).mode : momMode, minutes_available: true } };
        },
        createBackgroundAuth(callback) {
          signedOut = callback;
          return {
            async access() {
              calls.push('auth:start');
              if (deferAccess) await accessGate;
              if (expireAccess) { await signedOut(); throw new Error(userMessages.problem('MI-AUTH-105')); }
              clock += 5000; calls.push('auth:ready'); return 'access';
            },
            async publicState() { return { signedIn: true, userId }; },
          };
        },
      };
      return { BACKEND_HTTP_BASE_URL: 'https://example.com', isGoogleDriveCallbackUrl: () => false };
    },
  });
  return { send, calls, requests, offscreenStarts, local, releaseAccess, signOut: () => signedOut(), state: () => session.state,
    closeTab: id => chrome.tabs.onRemoved.fire(id), navigateTab: (id, url) => { meetingUrl = url; chrome.tabs.onUpdated.fire(id, { url }, {}); },
    leaveMeeting: () => { meetingStatus = 'not-in-meeting'; },
    get streams() { return streams; }, get active() { return active; } };
}

const start = { type: 'START_CAPTURE', tabId: 7 };
const stop = { type: 'STOP_CAPTURE' };
const tick = () => new Promise(resolve => setImmediate(resolve));

test('a slow sign-in refresh, audio-page creation and meeting probe finish before a fresh tab ID is minted', async () => {
  const h = harness();
  assert.equal((await h.send(start)).ok, true);
  const mint = h.calls.indexOf('mint:7');
  for (const ready of ['auth:ready', 'offscreen:ready', 'probe:7', 'tab:7']) assert.ok(h.calls.indexOf(ready) < mint, ready);
  assert.equal(h.active, true);
  assert.equal(h.state().captureStatus, 'capturing');
});

test('duplicate start clicks request only one tab stream and keep the first capture running', async () => {
  const h = harness();
  const [first, second] = await Promise.all([h.send(start), h.send(start)]);
  assert.equal(first.ok, true);
  assert.equal(second.ok, false);
  assert.match(second.error, /MI-CAP-002/);
  assert.equal(h.streams, 1);
  assert.equal(h.active, true);
});

test('a stop during slow start setup cancels the pending start before it obtains tab audio', async () => {
  const h = harness({ deferAccess: true });
  const started = h.send(start);
  await tick();
  const stopped = h.send(stop);
  await tick();
  h.releaseAccess();
  assert.match((await started).error, /MI-CAP-006/);
  assert.equal((await stopped).ok, true);
  assert.equal(h.streams, 0);
  assert.equal(h.active, false);
  assert.equal(h.state().captureStatus, 'idle');
});

test('repeated start and stop cycles consume one fresh ID each and release prior capture', async () => {
  const h = harness();
  for (let i = 0; i < 5; i++) {
    assert.equal((await h.send(start)).ok, true);
    assert.equal(h.active, true);
    assert.equal((await h.send(stop)).ok, true);
    assert.equal(h.active, false);
    assert.equal(h.state().captureStatus, 'idle');
  }
  assert.equal(h.streams, 5);
});

test('closing or leaving the meeting during slow start cannot start listening afterwards', async () => {
  for (const event of ['close', 'navigate']) {
    const h = harness({ deferAccess: true });
    const started = h.send(start);
    await tick();
    if (event === 'close') h.closeTab(7);
    else h.navigateTab(7, 'https://meet.google.com/xyz-abcd-efg');
    await tick();
    h.releaseAccess();
    assert.match((await started).error, /MI-CAP-006/, event);
    await tick();
    assert.equal(h.streams, 0, event);
    assert.equal(h.active, false, event);
    assert.equal(h.state().captureStatus, 'idle', event);
  }
});

test('capture preparation cannot reset an already active capture', async () => {
  const h = harness();
  await h.send(start);
  h.leaveMeeting();
  const result = await h.send({ type: 'PREPARE_CAPTURE', tabId: 7 });
  assert.match(result.error, /MI-CAP-002/);
  assert.equal(h.state().captureStatus, 'capturing');
  assert.equal(h.active, true);
});

test('restored active capture is checked before asking Chrome for another stream ID', async () => {
  const h = harness({ activeOffscreen: true, savedState: { captureStatus: 'capturing', meetingStatus: 'in-meeting', captureTabId: 7, captureSessionId: 'existing', backendConnected: true } });
  assert.match((await h.send(start)).error, /MI-CAP-002/);
  assert.equal(h.streams, 0);
  assert.equal(h.active, true);
});

test('browser invocation failures keep a safe support-coded error', async () => {
  const h = harness({ invocationDenied: true });
  const result = await h.send(start);
  assert.match(result.error, /MI-CAP-003/);
  assert.doesNotMatch(result.error, /activeTab|Extension has not been invoked/);
  assert.equal(h.active, false);
});

test('sign-out during refresh does not deadlock the start queue or acquire audio', async () => {
  const h = harness({ expireAccess: true });
  const result = await Promise.race([h.send(start), new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error('start queue deadlocked during sign-out')), 500);
    timer.unref();
  })]);
  assert.equal(result.ok, false);
  assert.match(result.error, /MI-AUTH-105/);
  assert.equal(h.streams, 0);
  assert.equal(h.active, false);
});

test('a failed permission request preserves remembered access unless the browser explicitly denies it', async () => {
  const h = harness();
  h.local[permissions.MICROPHONE_PERMISSION_KEY] = true;
  await h.send({ type: 'MIC_PERMISSION_RESULT', granted: false, errorName: 'NotAllowedError', permissionState: 'prompt' });
  assert.equal(h.local[permissions.MICROPHONE_PERMISSION_KEY], true);
  await h.send({ type: 'MIC_PERMISSION_RESULT', granted: false, errorName: 'NotAllowedError', permissionState: 'denied' });
  assert.equal(h.local[permissions.MICROPHONE_PERMISSION_KEY], false);
});


test('meeting preferences finish loading before stream minting for all three modes', async () => {
  for (const mode of ['live_only', 'mom_only', 'both']) {
    const h = harness({ momMode: mode });
    assert.equal((await h.send(start)).ok, true);
    assert.ok(h.calls.indexOf('mom:settings') < h.calls.indexOf('mint:7'));
    assert.equal(h.state().momSettings.mode, mode);
    await h.send(stop);
  }
});

test('saving a meeting mode never requests audio or clears the microphone grant', async () => {
  const h = harness();
  const reply = await h.send({type:'MOM_SETTINGS',mode:'mom_only',timezone:'Asia/Kolkata'});
  assert.equal(reply.ok, true);
  assert.equal(h.streams, 0);
  assert.equal(h.active, false);
  assert.deepEqual(h.local, {});
  assert.equal(JSON.parse(h.requests[0].body).timezone, 'Asia/Kolkata');
});

test('unavailable preferences fail before capturing audio and never expose raw service errors', async () => {
  const h = harness({ momFailure: true });
  const reply = await h.send(start);
  assert.equal(reply.ok, false);
  assert.match(reply.error, /MI-MOM-001/);
  assert.doesNotMatch(reply.error, /provider-secret/);
  assert.equal(h.streams, 0);
});

test('MOM metadata is fenced by user and old draining captures cannot replace the active rejoin', async () => {
  const h = harness({ momMode:'both' });
  await h.send(start);
  const sender={id:'extension',url:'chrome-extension://extension/offscreen-page.html'};
  const meeting={sessionId:'a'.repeat(32),roomId:'abc-defg-hij',captureId:h.state().captureSessionId,mode:'both',state:'active'};
  await h.send({type:'MOM_UPDATE',userId:'another-user',meeting},sender);
  assert.equal(h.state().minutes.length, 0);
  await h.send({type:'MOM_UPDATE',userId:'user',meeting},sender);
  await h.send({type:'MOM_UPDATE',userId:'user',meeting:{...meeting,captureId:'old-capture'}},sender);
  assert.equal(h.state().minutes[0].captureId, meeting.captureId);
  await h.send({type:'MOM_STATUS',sessionId:meeting.sessionId});
  assert.equal(h.state().minutes[0].state, 'sent');
  await h.send({type:'MOM_UPDATE',userId:'user',meeting},sender);
  assert.equal(h.state().minutes[0].state, 'sent');
  await h.signOut();
  assert.equal(h.state().minutes.length, 0);
  assert.equal(h.state().momSettings, undefined);
});

test('service-worker recovery drops recap metadata owned by another signed-in user', async () => {
  const h = harness({ savedState: { meetingStatus:'unknown',captureStatus:'idle',backendConnected:false,momUserId:'other',momSettings:{mode:'both'},minutes:[{sessionId:'a'.repeat(32)}] } });
  const state = await h.send({type:'GET_STATE'});
  assert.equal(state.minutes.length, 0);
  assert.equal(state.momSettings, undefined);
});

test('changing preferences during capture preserves the active meeting mode', async () => {
  const h = harness({ momMode:'live_only' });
  await h.send(start);
  await h.send({type:'MOM_SETTINGS',mode:'mom_only',timezone:'UTC'});
  assert.equal(h.state().momSettings.mode,'mom_only');
  assert.equal(h.state().captureMode,'live_only');
  assert.equal(h.active,true);
  assert.equal(h.streams,1);
  await h.send(stop);
});


test('Zoom starts with a namespaced room and switching to another room releases capture', async () => {
  const h = harness({ meetingUrl: 'https://app.zoom.us/wc/12345678901/join?pwd=private', momMode: 'both' });
  assert.equal((await h.send(start)).ok, true);
  assert.equal(h.offscreenStarts[0].roomId, 'zoom:12345678901');
  assert.equal(h.offscreenStarts[0].mode, 'both');
  h.navigateTab(7, 'https://us02web.zoom.us/wc/12345678901/join?pwd=rotated');
  await tick();
  assert.equal(h.active, true, 'same Zoom room survives host and query changes');
  h.navigateTab(7, 'https://app.zoom.us/wc/98765432101/join');
  await tick(); await tick();
  assert.equal(h.active, false);
});

test('Zoom waiting-room status cannot start microphone or tab capture', async () => {
  const h = harness({ meetingUrl: 'https://app.zoom.us/wc/12345678901/join' });
  h.leaveMeeting();
  const response = await h.send(start);
  assert.equal(response.ok, false);
  assert.equal(h.streams, 0);
  assert.equal(h.active, false);
});
