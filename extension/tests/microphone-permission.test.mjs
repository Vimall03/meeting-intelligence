import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveMicrophonePermission, isMicrophonePermissionError } from '../lib/microphone-permission.ts';

test('an explicit browser denial overrides a remembered successful request', () => {
  assert.equal(resolveMicrophonePermission('denied', true), 'denied');
  assert.equal(resolveMicrophonePermission('granted', false), 'granted');
});

test('a remembered grant survives an inconclusive sidepanel permission query', () => {
  assert.equal(resolveMicrophonePermission('prompt', true), 'granted');
  assert.equal(resolveMicrophonePermission(undefined, true), 'granted');
  assert.equal(resolveMicrophonePermission(undefined, false), 'prompt');
});
test('a new user still needs to enable the microphone', () => {
  assert.equal(resolveMicrophonePermission('prompt', false), 'prompt');
});

test('a busy or missing input device does not revoke a remembered browser grant', () => {
  assert.equal(isMicrophonePermissionError('NotReadableError'), false);
  assert.equal(isMicrophonePermissionError('NotFoundError'), false);
  assert.equal(isMicrophonePermissionError('NotAllowedError'), true);
  assert.equal(isMicrophonePermissionError('SecurityError'), true);
});
