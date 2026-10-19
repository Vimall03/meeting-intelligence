// Public build configuration. Only loopback development hosts may use HTTP.
function secureBase(value: string) {
  if (!value) return '';
  const url = new URL(value);
  if (url.username || url.password || url.search || url.hash || (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))) throw new Error('Invalid service configuration');
  return value.replace(/\/$/, '');
}
export const BACKEND_HTTP_BASE_URL = secureBase(import.meta.env.WXT_BACKEND_URL || 'http://localhost:8000');
export const AUTH_API_URL = secureBase(import.meta.env.WXT_AUTH_API_URL || '');
export const MEETING_SOCKET_URL = `${BACKEND_HTTP_BASE_URL.replace(/^http/, 'ws')}/ws/meeting`;
export const GOOGLE_DRIVE_STATUS_URL = `${BACKEND_HTTP_BASE_URL}/api/integrations/google-drive/status`;
export const GOOGLE_DRIVE_AUTH_START_URL = `${BACKEND_HTTP_BASE_URL}/api/integrations/google-drive/auth/start`;
export const GOOGLE_DRIVE_AUTH_TAB_KEY = 'googleDriveOAuthPending';
export const GOOGLE_DRIVE_AUTH_RESULT_KEY = 'googleDriveOAuthResult';
export const ATLASSIAN_STATUS_URL = `${BACKEND_HTTP_BASE_URL}/api/integrations/atlassian/status`;
export const ATLASSIAN_AUTH_START_URL = `${BACKEND_HTTP_BASE_URL}/api/integrations/atlassian/auth/start`;
export const ATLASSIAN_JIRA_PROJECTS_URL = `${BACKEND_HTTP_BASE_URL}/api/integrations/atlassian/jira/projects`;
export const ATLASSIAN_JIRA_TASKS_URL = `${BACKEND_HTTP_BASE_URL}/api/integrations/atlassian/jira/tasks`;

export function isGoogleDriveCallbackUrl(url: string, redirectUri: string) {
  try {
    const actual = new URL(url);
    const expected = new URL(redirectUri);
    return actual.origin === expected.origin && actual.pathname === expected.pathname;
  } catch {
    return false;
  }
}

// Each window is a self-contained WebM/Opus recording sent to the backend.
// Kept short so new speech reaches Jev and the pending-event enrichment
// buffer quickly instead of waiting on one long window.
export const AUDIO_MIME_TYPE = 'audio/webm;codecs=opus';
export const AUDIO_WINDOW_MS = 3_000;
// Experimental canary only. Both this build flag and server availability are required.
export const STREAMING_TRANSCRIPTION = import.meta.env.WXT_STREAMING_TRANSCRIPTION === 'true';
