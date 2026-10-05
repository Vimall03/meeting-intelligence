import { problem, userError } from './user-messages.ts';
export const authenticatedFetch: typeof fetch = async (input, init) => {
  const result = await chrome.runtime.sendMessage({ type: 'AUTH_FETCH', url: String(input), method: init?.method ?? 'GET', body: init?.body }) as { ok: boolean; error?: string; status: number; data: unknown };
  if (!result.ok) throw new Error(userError(result.error, 'MI-AUTH-104'));
  if (!result.status) throw new Error(problem('MI-CON-001'));
  return new Response(result.status === 204 ? null : JSON.stringify(result.data), { status: result.status, headers: { 'Content-Type': 'application/json' } });
};
