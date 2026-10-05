import test from 'node:test';
import assert from 'node:assert/strict';
import { AuthSession } from '../lib/auth-session.ts';
const session = { accessToken: 'old-access', refreshToken: 'old-refresh', expiresIn: 900, expiresAt: 0, userId: 'A', email: 'a@example.com' };
function fixture(call, value = session) {
  let stored = value, stops = 0;
  const manager = new AuthSession({ read: async () => stored, write: async v => { stored = v; }, clear: async () => { stored = undefined; } }, call, async () => { stops++; });
  return { manager, stored: () => stored, stops: () => stops };
}
const tokens = { accessToken: 'new-access', refreshToken: 'new-refresh', expiresIn: 900 };

test('concurrent requests share one rotation and persist its replacement before proceeding', async () => {
  let calls = 0;
  const f = fixture(async (path, body) => { assert.equal(path, '/auth/refresh'); assert.equal(body.refreshToken, 'old-refresh'); calls++; await new Promise(r => setTimeout(r, 5)); return { status: 200, data: tokens }; });
  const result = await Promise.all(Array.from({ length: 20 }, () => f.manager.access()));
  assert.equal(calls, 1); assert.ok(result.every(v => v === 'new-access'));
  assert.equal(f.stored().refreshToken, 'new-refresh'); assert.equal(f.stops(), 0);
});

test('late rotation cannot resurrect a signed-out user', async () => {
  let finish;
  const pending = new Promise(r => { finish = r; });
  const f = fixture(async () => { await pending; return { status: 200, data: tokens }; });
  const refresh = f.manager.access(); await new Promise(r => setImmediate(r));
  await f.manager.clear(); finish(); await assert.rejects(refresh, /MI-AUTH-105/);
  assert.equal(f.stored(), undefined); assert.equal((await f.manager.publicState()).signedIn, false);
});

test('temporary refresh outage retains the session and does not stop capture', async () => {
  const f = fixture(async () => ({ status: 503, data: {} }));
  await assert.rejects(f.manager.access(), /MI-AUTH-104/);
  assert.equal(f.stored().refreshToken, 'old-refresh'); assert.equal(f.stops(), 0);
});

test('revoked refresh clears credentials and stops capture once', async () => {
  const f = fixture(async () => ({ status: 401, data: {} }));
  await assert.rejects(f.manager.access(), /MI-AUTH-105/);
  assert.equal(f.stored(), undefined); assert.equal(f.stops(), 1);
});

test('an old user response is rejected after sign-out', async () => {
  const f = fixture(async () => ({ status: 200, data: tokens }), { ...session, expiresAt: Date.now() + 900_000 });
  let finish; const ready = new Promise(r => { finish = r; });
  const operation = f.manager.authorized(async () => { await ready; return { status: 200, data: { files: ['A-private'] } }; });
  await new Promise(r => setImmediate(r)); await f.manager.clear(); finish();
  await assert.rejects(operation, /MI-AUTH-105/);
});

test('provider reconnects and uncertain task writes are never automatically repeated', async () => {
  let calls = 0;
  const f = fixture(async () => { throw new Error('unexpected auth call'); }, { ...session, expiresAt: Date.now() + 900_000 });
  const result = await f.manager.authorized(async () => { calls++; return { status: 401, data: { detail: { support_code: 'MI-AUTH-001' } } }; });
  assert.equal(result.status, 401); assert.equal(calls, 1);
  await assert.rejects(f.manager.authorized(async () => { calls++; throw new Error('network uncertain'); }));
  assert.equal(calls, 2);
});

test('a genuine app auth rejection refreshes and retries only once', async () => {
  const f = fixture(async () => ({ status: 200, data: tokens }), { ...session, expiresAt: Date.now() + 900_000 });
  const seen = [];
  await f.manager.authorized(async access => { seen.push(access); return access === 'old-access' ? { status: 401, data: { detail: { support_code: 'MI-AUTH-105' } } } : { status: 200, data: {} }; });
  assert.deepEqual(seen, ['old-access', 'new-access']);
});

test('logout waits for rotation and revokes its replacement, then clears locally', async () => {
  let finish; const ready = new Promise(r => { finish = r; }); const calls = [];
  const f = fixture(async (path, body) => { calls.push([path, body]); if (path === '/auth/refresh') { await ready; return { status: 200, data: tokens }; } return { status: 200, data: {} }; });
  const rotate = f.manager.access(); await new Promise(r => setImmediate(r));
  const logout = f.manager.logout(); finish(); await Promise.all([rotate, logout]);
  assert.equal(calls[1][1].refreshToken, 'new-refresh'); assert.equal(f.stored(), undefined); assert.equal(f.stops(), 1);
});

test('wrong OTP advances the opaque session and does not resend an email', async () => {
  const calls = [];
  const f = fixture(async (path, body) => { calls.push([path, body]); return path.endsWith('request') ? { status: 202, data: { session: 'first', resendAfter: 60 } } : { status: 401, data: { session: 'advanced', error: { support_code: 'MI-AUTH-103' } } }; }, undefined);
  // undefined selects the default fixture argument; explicitly clear for signup.
  await f.manager.clear();
  await f.manager.requestCode('person@example.com');
  await assert.rejects(f.manager.verifyCode('000000'), /MI-AUTH-103/);
  await assert.rejects(f.manager.verifyCode('000001'), /MI-AUTH-103/);
  assert.equal(calls[2][1].session, 'advanced'); assert.equal(calls.filter(c => c[0].endsWith('request')).length, 1);
});
