import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

function harness() {
  const messages = [];
  let listener;
  let status = 'in-meeting';
  let mediaRequests = 0;
  const roomId = 'abc-defg-hij';
  const context = {
    isValid: true,
    onInvalidated() {},
    setTimeout() {},
    setInterval() {},
  };
  class Observer {
    constructor(callback) { this.callback = callback; }
    observe() {}
    disconnect() {}
  }
  const source = fs.readFileSync(new URL('../entrypoints/meet.content.ts', import.meta.url), 'utf8');
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, {
    exports: {},
    require(name) {
      if (name.includes('meeting-detection')) return { detectMeeting: () => status };
      if (name.includes('meeting-session')) return { meetingIdFromUrl: () => roomId };
      throw new Error(`Unexpected dependency: ${name}`);
    },
    defineContentScript: specification => specification.main(context),
    window: { location: { href: `https://meet.google.com/${roomId}` } },
    document: {
      documentElement: {},
      // Hidden/stale Meet warnings used to become a microphone failure notice.
      querySelectorAll: () => [{ getAttribute: () => 'Microphone problem' }],
    },
    navigator: { mediaDevices: { getUserMedia() { mediaRequests++; } } },
    MutationObserver: Observer,
    chrome: { runtime: {
      onMessage: { addListener(callback) { listener = callback; }, removeListener() {} },
      async sendMessage(message) { messages.push(message); },
    } },
  });
  return {
    messages,
    setStatus(next) { status = next; },
    mediaRequests: () => mediaRequests,
    async probe() {
      const result = await new Promise(resolve => {
        assert.equal(listener({ type: 'GET_MEETING_STATUS' }, {}, resolve), true);
      });
      return JSON.parse(JSON.stringify(result));
    },
  };
}

test('Meet microphone warning text does not report extension microphone failure or request microphone access', async () => {
  const h = harness();
  assert.deepEqual(await h.probe(), { status: 'in-meeting', meetingId: 'abc-defg-hij' });
  assert.equal(h.messages.length, 2);
  for (const message of h.messages) {
    assert.equal(message.type, 'MEETING_STATUS');
    assert.equal(message.status, 'in-meeting');
    assert.equal(message.meetingId, 'abc-defg-hij');
    assert.equal('meetMicrophoneProblem' in message, false);
  }
  assert.equal(h.mediaRequests(), 0);
});

test('meeting probe still reports leaving the room while a Meet microphone warning remains', async () => {
  const h = harness();
  h.setStatus('not-in-meeting');
  assert.deepEqual(await h.probe(), { status: 'not-in-meeting' });
  const report = h.messages.at(-1);
  assert.equal(report.status, 'not-in-meeting');
  assert.equal(report.meetingId, undefined);
  assert.equal('meetMicrophoneProblem' in report, false);
});
