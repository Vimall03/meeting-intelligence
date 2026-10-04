import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveMicrophonePermission, isMicrophonePermissionError } from '../lib/microphone-permission.ts';

test('the browser permission state overrides a stale remembered hint', () => {
  assert.equal(resolveMicrophonePermission('prompt', true), 'prompt');
  assert.equal(resolveMicrophonePermission('denied', true), 'denied');
  assert.equal(resolveMicrophonePermission('granted', false), 'granted');
});

test('a remembered grant is only used when the Permissions API is unavailable', () => {
  assert.equal(resolveMicrophonePermission(undefined, true), 'granted');
  assert.equal(resolveMicrophonePermission(undefined, false), 'prompt');
});
test('explicit revocation wins; a new user still needs to enable the microphone', () => {
  assert.equal(resolveMicrophonePermission('prompt', false), 'prompt');
});

test('a busy or missing input device does not revoke a remembered browser grant', () => {
  assert.equal(isMicrophonePermissionError('NotReadableError'), false);
  assert.equal(isMicrophonePermissionError('NotFoundError'), false);
  assert.equal(isMicrophonePermissionError('NotAllowedError'), true);
  assert.equal(isMicrophonePermissionError('SecurityError'), true);
});
