import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { isTimelineSnapshot, visibleTimelineItems } from '../lib/timeline.ts';

const backendDirectory = fileURLToPath(new URL('../../backend/', import.meta.url));

test('backend result snapshots reach the extension as newest-first cards', () => {
  const script = `
import json
from app.services.meeting_timeline_service import MeetingTimelineService

timeline = MeetingTimelineService()
timeline.add_task(title="Send survey link", description="Ask Jessica to send it to Fabio.", confidence=0.85)
timeline.add_search(query="customer interview spreadsheet", files=[{
    "id": "file-1",
    "name": "Interview contacts",
    "mime_type": "application/vnd.google-apps.spreadsheet",
    "matched_terms": ["interview"],
    "relevance_score": 4.5,
}])
print(json.dumps(timeline.snapshot()))
print(json.dumps(MeetingTimelineService().snapshot()))
`;
  const result = spawnSync('python3', ['-c', script], { cwd: backendDirectory, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const [snapshot, nextMeeting] = result.stdout.trim().split('\n').map(JSON.parse);

  assert.equal(isTimelineSnapshot(snapshot), true);
  assert.equal(snapshot.revision, 2);
  assert.deepEqual(snapshot.items.map((item) => item.kind), ['search_results', 'task_suggestion']);
  assert.equal(snapshot.items[0].source.label, 'Google Drive');
  assert.equal(snapshot.items[0].files[0].name, 'Interview contacts');
  assert.equal(snapshot.items[1].title, 'Send survey link');
  assert.equal(snapshot.items[1].description, 'Ask Jessica to send it to Fabio.');
  assert.equal(isTimelineSnapshot(nextMeeting), true);
  assert.equal(nextMeeting.items.length, 0);
  assert.notEqual(nextMeeting.session_id, snapshot.session_id);
});

test('extension rejects malformed or out-of-order timeline snapshots', () => {
  const item = {
    id: 'session:1', sequence: 1, created_at: '2026-09-27T00:00:00Z',
    kind: 'task_suggestion', title: 'Follow up', description: 'Call the customer.', confidence: 0.8,
  };
  const base = { type: 'timeline_snapshot', session_id: 'session', revision: 1, items: [item] };
  assert.equal(isTimelineSnapshot(base), true);
  assert.equal(isTimelineSnapshot({ ...base, items: [{ ...item, description: '' }] }), false);
  assert.equal(isTimelineSnapshot({ ...base, items: [{ ...item, sequence: 2 }, item] }), true);
  assert.equal(isTimelineSnapshot({ ...base, items: [item, { ...item, sequence: 2 }] }), false);
});

test('extension hides empty search entries from older snapshots', () => {
  const task = {
    id: 'session:1', sequence: 1, created_at: '2026-09-27T00:00:00Z',
    kind: 'task_suggestion', title: 'Follow up', description: 'Call the customer.', confidence: 0.8,
  };
  const emptySearch = {
    id: 'session:2', sequence: 2, created_at: '2026-09-27T00:00:01Z',
    kind: 'search_results', source: { id: 'google_drive', label: 'Google Drive' },
    query: 'customer', files: [], error: null,
  };
  const snapshot = { type: 'timeline_snapshot', session_id: 'session', revision: 2, items: [emptySearch, task] };
  assert.equal(isTimelineSnapshot(snapshot), true);
  assert.deepEqual(visibleTimelineItems(snapshot), [task]);
});
