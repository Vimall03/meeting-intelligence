import test from 'node:test';
import assert from 'node:assert/strict';
import { loadJiraProjects, submitJiraTask } from '../lib/jira-tasks.ts';

test('project picker reads the personal Jira project list', async () => {
  let request;
  const projects = await loadJiraProjects('http://localhost/projects', async (url, options) => {
    request = { url, options };
    return new Response(JSON.stringify({ projects: [{ id: '10001', key: 'BTS', name: 'Bug Tracking System' }] }), { status: 200 });
  });
  assert.deepEqual(projects.map(({ key }) => key), ['BTS']);
  assert.equal(request.url, 'http://localhost/projects');
  assert.equal(request.options.cache, 'no-store');
});

test('explicit create sends the selected project and returns a safe Jira link', async () => {
  let request;
  const created = await submitJiraTask('http://localhost/tasks', {
    project_id: '10001', title: 'Meeting follow-up', description: 'Send notes',
  }, async (url, options) => {
    request = { url, options };
    return new Response(JSON.stringify({ key: 'BTS-10', project_key: 'BTS', url: 'https://auriqis-dev.atlassian.net/browse/BTS-10' }), { status: 201 });
  });
  assert.equal(request.options.method, 'POST');
  assert.deepEqual(JSON.parse(request.options.body), { project_id: '10001', title: 'Meeting follow-up', description: 'Send notes' });
  assert.equal(created.key, 'BTS-10');
});

test('create reports Jira failure and rejects an unexpected task URL', async () => {
  const input = { project_id: '10001', title: 'Follow-up', description: 'Notes' };
  await assert.rejects(submitJiraTask('http://localhost/tasks', input, async () =>
    new Response(JSON.stringify({ detail: { message: 'Project requires another field' } }), { status: 502 })
  ), /requires another field/);
  await assert.rejects(submitJiraTask('http://localhost/tasks', input, async () =>
    new Response(JSON.stringify({ key: 'BTS-10', project_key: 'BTS', url: 'https://example.com/browse/BTS-10' }), { status: 201 })
  ), /invalid task link/);
});


test('network failures explain backend recovery and avoid suggesting blind duplicate creation', async () => {
  const fail = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(loadJiraProjects('http://localhost/projects', fail), /Start it in VS Code/);
  await assert.rejects(submitJiraTask('http://localhost/tasks', { project_id: '1', title: 'Fix export', description: 'Last row missing' }, fail), /Check Jira before retrying/);
});
