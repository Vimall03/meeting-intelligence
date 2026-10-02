export type JiraProject = { id: string; key: string; name: string };
export type CreatedJiraTask = { key: string; url: string; project_key: string };

type ErrorPayload = { detail?: { message?: string } };

export async function loadJiraProjects(url: string, fetcher: typeof fetch = fetch): Promise<JiraProject[]> {
  const response = await fetcher(url, { cache: 'no-store' }).catch(() => {
    throw new Error('Could not reach the local backend. Start it in VS Code, then reopen the project picker.');
  });
  const payload = await response.json().catch(() => ({})) as ErrorPayload & { projects?: JiraProject[] };
  if (!response.ok) throw new Error(payload.detail?.message ?? `Could not load Jira projects (${response.status}).`);
  if (!Array.isArray(payload.projects) || !payload.projects.every((project) =>
    project && typeof project.id === 'string' && typeof project.key === 'string' && typeof project.name === 'string'
  )) throw new Error('Jira returned an invalid project list.');
  return payload.projects;
}

export async function submitJiraTask(url: string, task: { project_id: string; title: string; description: string }, fetcher: typeof fetch = fetch): Promise<CreatedJiraTask> {
  const response = await fetcher(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(task),
  }).catch(() => {
    throw new Error('Lost connection to the local backend. Make sure it is running in VS Code. Check Jira before retrying in case the task was already created.');
  });
  const payload = await response.json().catch(() => ({})) as ErrorPayload & Partial<CreatedJiraTask>;
  if (!response.ok) throw new Error(payload.detail?.message ?? `Jira task creation failed (${response.status}).`);
  try {
    const taskUrl = new URL(payload.url ?? '');
    if (typeof payload.key !== 'string' || !/^[A-Z][A-Z0-9_]*-\d+$/.test(payload.key)
      || taskUrl.protocol !== 'https:' || !taskUrl.hostname.endsWith('.atlassian.net')
      || taskUrl.pathname !== `/browse/${payload.key}` || typeof payload.project_key !== 'string') {
      throw new Error('invalid task');
    }
  } catch {
    throw new Error('Jira returned an invalid task link. Check the project before retrying.');
  }
  return payload as CreatedJiraTask;
}
