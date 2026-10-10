import test from 'node:test';
import assert from 'node:assert/strict';
import { defaultMomSettings, parseMomSettings, updateMomMeeting, momProblem } from '../lib/mom.ts';
import { problem } from '../lib/user-messages.ts';

test('new users stay live-only and unavailable or malformed preferences cannot opt in', () => {
  assert.equal(defaultMomSettings.mode, 'live_only');
  assert.equal(defaultMomSettings.minutes_available, false);
  for (const value of [undefined, {}, { ...defaultMomSettings, mode:'unexpected' }, { ...defaultMomSettings, grace_seconds:NaN }]) assert.throws(() => parseMomSettings(value));
});

test('MOM errors use catalog copy and discard remote details', () => {
  assert.equal(momProblem({detail:{support_code:'MI-MOM-006',message:'secret'}}),problem('MI-MOM-006'));
  assert.equal(momProblem({detail:{support_code:'MI-OTHER-006',message:'secret'}}),problem('MI-MOM-001'));
});

test('recap metadata stays bounded without duplicating sessions', () => {
  let rows=[];
  for (let n=0;n<12;n++) rows=updateMomMeeting(rows,{sessionId:String(n),state:'active'});
  assert.equal(rows.length,8);
  rows=updateMomMeeting(rows,{sessionId:'11',state:'sent'});
  assert.equal(rows.length,8);
  assert.equal(rows.filter(r=>r.sessionId==='11').length,1);
  assert.equal(rows[0].state,'sent');
});
