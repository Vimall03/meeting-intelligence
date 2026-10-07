import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import * as userMessages from '../lib/user-messages.ts';

function harness({ permissionState = 'prompt', failFirstRequest = false, deferRequest = false, closeAfterGrant = false } = {}) {
  const messages = [], requests = [];
  const elements = new Map();
  for (const selector of ['#status', '#help', '.close', '#request-microphone']) {
    elements.set(selector, { textContent: '', disabled: false, handlers: {}, addEventListener(name, handler) { this.handlers[name] = handler; } });
  }
  const track = { stopped: false, stop() { this.stopped = true; } };
  const stream = { getTracks: () => [track] };
  let resolveRequest;
  const pendingRequest = new Promise(resolve => { resolveRequest = resolve; });
  const source = fs.readFileSync(new URL('../entrypoints/mic-permission/main.ts', import.meta.url), 'utf8');
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, {
    exports: {}, require(name) { return name.includes('user-messages') ? userMessages : {}; }, Error, DOMException,
    document: { querySelector: selector => elements.get(selector) },
    navigator: {
      mediaDevices: { async getUserMedia(options) {
        requests.push(options);
        if (failFirstRequest && requests.length === 1) throw new DOMException('Private browser/device details', 'NotAllowedError');
        return deferRequest ? pendingRequest : stream;
      } },
      permissions: { async query() { return { state: permissionState }; } },
    },
    chrome: { runtime: { async sendMessage(message) {
      messages.push(message);
      if (closeAfterGrant && message.granted) throw new Error('Extension context invalidated');
      return { ok: true };
    } } },
  });
  return {
    messages, requests, track,
    button: elements.get('#request-microphone'), status: elements.get('#status'), help: elements.get('#help'),
    click() { elements.get('#request-microphone').handlers.click(); },
    close() { elements.get('.close').handlers.click(); },
    resolveRequest() { resolveRequest(stream); },
    async settle() { await new Promise(resolve => setImmediate(resolve)); },
  };
}

test('setup explains persistent Brave permission before any microphone request', async () => {
  const h = harness({ deferRequest: true });
  const html = fs.readFileSync(new URL('../entrypoints/mic-permission/index.html', import.meta.url), 'utf8');
  assert.match(html, /Remember my decision/);
  assert.match(html, /forever/);
  assert.match(html, /id="request-microphone"[^>]*>Continue/);
  assert.equal(h.requests.length, 0);
  h.click();
  h.click();
  assert.equal(h.requests.length, 1);
  assert.equal(h.button.disabled, true);
  h.resolveRequest();
  await h.settle();
  assert.equal(h.track.stopped, true);
  assert.deepEqual(h.messages.map(message => message.granted), [true]);
});

test('a denied request stays on setup with safe coded copy and permits a same-page retry', async () => {
  const h = harness({ failFirstRequest: true, permissionState: 'denied' });
  h.click();
  await h.settle();
  assert.match(h.status.textContent, /MI-MIC-001/);
  assert.doesNotMatch(h.status.textContent, /Private browser|NotAllowedError/);
  assert.equal(h.button.disabled, false);
  assert.equal(h.button.textContent, 'Try again');
  assert.equal(h.messages[0].permissionState, 'denied');
  assert.equal(h.messages[0].granted, false);
  h.click();
  await h.settle();
  assert.equal(h.requests.length, 2);
  assert.equal(h.messages[1].granted, true);
  assert.equal(h.track.stopped, true);
});

test('a tab closing after successful permission cannot report a false denial', async () => {
  const h = harness({ closeAfterGrant: true });
  h.click();
  await h.settle();
  assert.deepEqual(h.messages.map(message => message.granted), [true]);
  assert.equal(h.status.textContent, 'Microphone access granted.');
  assert.equal(h.track.stopped, true);
});

test('closing setup before continuing never activates the microphone', async () => {
  const h = harness();
  h.close();
  await h.settle();
  assert.equal(h.requests.length, 0);
  assert.deepEqual(h.messages.map(message => message.type), ['CLOSE_MIC_PERMISSION_TAB']);
});
