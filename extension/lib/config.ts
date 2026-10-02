// Keep this in one place so the local endpoint can be replaced later.
export const MEETING_SOCKET_URL = 'ws://localhost:8000/ws/meeting';
export const BACKEND_HTTP_BASE_URL = 'http://localhost:8000';
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
