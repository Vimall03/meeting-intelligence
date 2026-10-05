import { AuthSession, type Session } from './auth-session';
import { AUTH_API_URL, BACKEND_HTTP_BASE_URL } from './config';
import { problem } from './user-messages';

export function createBackgroundAuth(onSignedOut: () => Promise<void>) {
  // Set immediately on worker startup, before restoring any stored credentials.
  const protectedStorage = Promise.all([
    chrome.storage.local.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' }),
    chrome.storage.session.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' }),
  ]);
  const sessions = new AuthSession({
    read: async () => { await protectedStorage; return (await chrome.storage.local.get('appSession')).appSession as Session | undefined; },
    write: async value => { await protectedStorage; await chrome.storage.local.set({ appSession: value }); },
    clear: async () => { await protectedStorage; await chrome.storage.local.remove(['appSession', 'googleDriveOAuthPending', 'googleDriveOAuthResult']); },
  }, async (path, body, access) => {
    if (!AUTH_API_URL) throw new Error(problem('MI-AUTH-109'));
    try {
      const response = await fetch(`${AUTH_API_URL}${path}`, { method: body === undefined ? 'GET' : 'POST',
        headers: { 'Content-Type': 'application/json', ...(access ? { Authorization: `Bearer ${access}` } : {}) },
        body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', signal: AbortSignal.timeout(12_000) });
      return { status: response.status, data: await response.json().catch(() => ({})) };
    } catch { throw new Error(problem('MI-AUTH-104')); }
  }, onSignedOut);
  return sessions;
}

export async function backendFetch(sessions: AuthSession, url: string, method = 'GET', body?: string) {
  const target = new URL(url);
  const base = new URL(BACKEND_HTTP_BASE_URL);
  if (target.origin !== base.origin || !target.pathname.startsWith('/api/') || !['GET', 'POST'].includes(method)
      || target.username || target.password || (body && body.length > 16_384)) throw new Error(problem('MI-AUTH-106'));
  return sessions.authorized(async access => {
    try {
      const response = await fetch(target, { method, body, headers: { Authorization: `Bearer ${access}`, 'Content-Type': 'application/json' },
        cache: 'no-store', signal: AbortSignal.timeout(20_000) });
      return { status: response.status, data: response.status === 204 ? {} : await response.json().catch(() => ({})) };
    } catch { throw new Error(problem(method === 'POST' && target.pathname.endsWith('/jira/tasks') ? 'MI-JIR-004' : 'MI-CON-001')); }
  });
}
