import test from 'node:test';
import assert from 'node:assert/strict';
import { userMessages, problem, userError, httpProblem, microphoneProblem } from '../lib/user-messages.ts';

test('every approved error has a stable support code and user-facing copy', () => {
  for (const [code, text] of Object.entries(userMessages)) {
    assert.match(code, /^MI-[A-Z]{3,4}-\d{3}$/);
    assert.ok(text.length > 20);
    assert.doesNotMatch(text, /localhost|VS Code|backend|OAuth|WebM|NotAllowedError|stack trace/i);
    assert.match(problem(code), new RegExp(`Code: ${code}`));
    assert.equal(userError(problem(code)), problem(code));
  }
});

test('unexpected upstream errors never leak details or forged codes', () => {
  for (const input of [new Error('Failed to fetch localhost:8000 secret-token'), 'Provider debug details (Code: MI-JIR-003)', undefined, { detail: 'secret' }]) {
    assert.equal(userError(input, 'MI-JIR-003'), problem('MI-JIR-003'));
  }
});

test('HTTP errors explain renewal, permissions, rate limits, and task uncertainty', () => {
  assert.equal(httpProblem(401, 'MI-JIR-003'), problem('MI-AUTH-001'));
  assert.equal(httpProblem(403, 'MI-JIR-003'), problem('MI-AUTH-002'));
  assert.equal(httpProblem(429, 'MI-JIR-003'), problem('MI-CON-003'));
  assert.equal(httpProblem(502, 'MI-JIR-003'), problem('MI-JIR-003'));
  assert.match(problem('MI-JIR-004'), /Check Jira before retrying/);
});

test('microphone errors distinguish blocked access from busy or missing devices', () => {
  assert.equal(microphoneProblem('NotAllowedError'), problem('MI-MIC-001'));
  assert.equal(microphoneProblem('NotFoundError'), problem('MI-MIC-003'));
  assert.equal(microphoneProblem('NotReadableError'), problem('MI-MIC-002'));
});
