import assert from 'node:assert/strict';
import test from 'node:test';
import { meetingIdFromUrl, meetingPlatformFromUrl, didMeetingEnd, isTimelineForMeeting } from '../lib/meeting-session.ts';
import { detectMeeting } from '../lib/meeting-detection.ts';
import { meetingContentScriptFiles, probeWithContentScriptRecovery } from '../lib/content-script-recovery.ts';

function page(visible, hidden = []) {
  return { querySelectorAll: () => [...visible.map(label => [label, true]), ...hidden.map(label => [label, false])].map(([label, shown]) => ({
    innerText: label,
    getAttribute: name => name === 'aria-label' ? label : null,
    closest: () => shown ? null : {},
    getClientRects: () => shown ? [{}] : [],
  })) };
}
const zoom = 'https://app.zoom.us/wc/12345678901/join?pwd=private-passcode';
const toolbar = ['Mute (Alt+A)', 'Stop Video', 'Participants (2)', 'Share Screen', 'Leave'];

test('Zoom Web App routes share an identity without retaining passcodes or account details', () => {
  for (const url of [zoom, 'https://us02web.zoom.us/wc/join/12345678901', 'https://company.zoom.us/wc/12345678901/start/', 'https://zoom.us/wc/join?confno=12345678901&pwd=another-secret']) {
    assert.equal(meetingPlatformFromUrl(url), 'zoom');
    assert.equal(meetingIdFromUrl(url), 'zoom:12345678901');
  }
});

test('Zoom identity rejects landing pages, spoofed hosts, unexpected schemes and malformed meeting numbers', () => {
  for (const url of ['https://zoom.us/j/12345678901', 'https://app.zoom.us/wc/join', 'https://app.zoom.us/wc/123/join', 'https://app.zoom.us/wc/123456789012/join', 'https://app.zoom.us/wc/join?confno=12345678901%23other', 'https://zoom.us.attacker.test/wc/12345678901/join', 'https://fakezoom.us/wc/12345678901/join', 'http://zoom.us/wc/12345678901/join', 'https://user:secret@zoom.us/wc/12345678901/join', 'https://zoom.us:8443/wc/12345678901/join']) {
    assert.equal(meetingIdFromUrl(url), undefined, url);
  }
});

test('Zoom detection requires the in-call toolbar, not preview, waiting-room or stale hidden controls', () => {
  assert.equal(detectMeeting(zoom, page(toolbar)), 'in-meeting');
  assert.equal(detectMeeting(zoom, page(['Unmute', 'Participants', 'End Meeting'])), 'in-meeting');
  assert.equal(detectMeeting(zoom, page(['Join Audio', 'Participants', 'Leave'])), 'in-meeting');
  assert.equal(detectMeeting(zoom, page(['Mute', 'More', 'Leave'])), 'in-meeting');
  assert.equal(detectMeeting(zoom, page(['Join', ...toolbar])), 'in-meeting', 'a breakout invitation does not end the current capture');
  for (const controls of [['Mute', 'Join'], ['Leave', 'Test speaker and microphone'], ['Leave', 'Mute'], []]) {
    assert.equal(detectMeeting(zoom, page(controls)), 'not-in-meeting');
  }
  assert.equal(detectMeeting(zoom, page([], toolbar)), 'not-in-meeting');
  assert.equal(detectMeeting('https://app.zoom.us/wc/join', page(toolbar)), 'not-in-meeting');
});

test('Meet detection keeps its existing room and pre-join behavior', () => {
  const meet = 'https://meet.google.com/abc-defg-hij';
  assert.equal(meetingIdFromUrl(meet), 'abc-defg-hij');
  assert.equal(detectMeeting(meet, page(['Turn off microphone', 'Leave call'])), 'in-meeting');
  assert.equal(detectMeeting(meet, page(['Turn off microphone', 'Join now'])), 'not-in-meeting');
  assert.equal(detectMeeting(meet, page([], ['Leave call', 'Turn off microphone'])), 'not-in-meeting');
});

test('switching platforms or Zoom rooms isolates timelines and ends the prior capture', () => {
  const owner = { tabId: 12, meetingId: 'zoom:12345678901' };
  assert.equal(isTimelineForMeeting(owner, 12, meetingIdFromUrl(zoom), 'in-meeting'), true);
  assert.equal(isTimelineForMeeting(owner, 13, owner.meetingId, 'in-meeting'), false);
  assert.equal(didMeetingEnd(owner, 12, 'in-meeting', 'abc-defg-hij'), true);
  assert.equal(didMeetingEnd(owner, 12, 'in-meeting', 'zoom:98765432101'), true);
  assert.equal(didMeetingEnd(owner, 12, 'in-meeting', owner.meetingId), false);
  assert.equal(didMeetingEnd(owner, 12, 'not-in-meeting', undefined), true);
});

test('Zoom recovery reinjects only the declared meeting adapter on an allowed Zoom Web App page', async () => {
  const manifest = { content_scripts: [
    { matches: ['https://example.com/*'], js: ['unrelated.js'] },
    { matches: ['https://meet.google.com/*', 'https://*.zoom.us/wc/*'], js: ['content-scripts/meeting.js'] },
  ] };
  assert.deepEqual(meetingContentScriptFiles(manifest, zoom), ['content-scripts/meeting.js']);
  assert.deepEqual(meetingContentScriptFiles(manifest, 'https://example.com/wc/12345678901/join'), []);
  let calls = 0;
  const injected = [];
  const result = await probeWithContentScriptRecovery({ manifest, tabUrl: zoom,
    probe: async () => { if (++calls === 1) throw new Error('stale script'); return { status: 'in-meeting', meetingId: 'zoom:12345678901' }; },
    inject: async files => injected.push(files),
  });
  assert.equal(result.meetingId, 'zoom:12345678901');
  assert.deepEqual(injected, [['content-scripts/meeting.js']]);
  await assert.rejects(probeWithContentScriptRecovery({ manifest, tabUrl: 'https://zoom.us.attacker.test/wc/12345678901/join',
    probe: async () => { throw new Error('untrusted page'); }, inject: async () => assert.fail('must not inject'),
  }), /untrusted page/);
});
