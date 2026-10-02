import assert from 'node:assert/strict';
import test from 'node:test';

import { didMeetingEnd, isTimelineForMeeting, meetingIdFromUrl } from '../lib/meeting-session.ts';

const owner = { tabId: 12, meetingId: 'abc-defg-hij' };

test('Meet room identity ignores query strings and trailing slashes', () => {
  assert.equal(meetingIdFromUrl('https://meet.google.com/ABC-DEFG-HIJ/?authuser=1'), 'abc-defg-hij');
  assert.equal(meetingIdFromUrl('https://meet.google.com/'), undefined);
  assert.equal(meetingIdFromUrl('https://example.com/abc-defg-hij'), undefined);
});

test('a timeline is visible only in its own active Meet room and tab', () => {
  assert.equal(isTimelineForMeeting(owner, 12, 'abc-defg-hij', 'in-meeting'), true);
  assert.equal(isTimelineForMeeting(owner, 12, 'xyz-abcd-efg', 'in-meeting'), false);
  assert.equal(isTimelineForMeeting(owner, 13, 'abc-defg-hij', 'in-meeting'), false);
  assert.equal(isTimelineForMeeting(owner, 12, 'abc-defg-hij', 'not-in-meeting'), false);
});

test('leaving or changing the owning room ends its timeline', () => {
  assert.equal(didMeetingEnd(owner, 12, 'not-in-meeting', undefined), true);
  assert.equal(didMeetingEnd(owner, 12, 'in-meeting', 'xyz-abcd-efg'), true);
  assert.equal(didMeetingEnd(owner, 12, 'in-meeting', 'abc-defg-hij'), false);
  assert.equal(didMeetingEnd(owner, 12, 'unknown', undefined), false);
  assert.equal(didMeetingEnd(owner, 12, 'in-meeting', undefined), false);
  assert.equal(didMeetingEnd(owner, 13, 'in-meeting', 'xyz-abcd-efg'), false);
});
