/** All product errors and recovery notices come from this catalog.
 * Codes are stable support identifiers. Keep browser/API details in diagnostic
 * logs; never pass provider messages directly to the interface.
 */
export const userMessages = {
  'MI-APP-001': 'Something went wrong. Try again. If it keeps happening, share this code with support.',
  'MI-APP-002': 'We couldn’t update this meeting. Refresh the meeting tab and try again.',
  'MI-APP-003': 'We couldn’t close this panel. Use the close button in your browser’s side panel.',
  'MI-CAP-001': 'Join a Google Meet call, then start listening. Other meeting platforms aren’t supported yet.',
  'MI-CAP-002': 'You’re already listening to a meeting. Stop that session before starting another.',
  'MI-CAP-003': 'We couldn’t access this meeting’s audio. Open Meeting Intelligence from the toolbar on your meeting tab and try again.',
  'MI-CAP-004': 'We couldn’t start listening. Refresh your meeting tab and try again.',
  'MI-CAP-005': 'We couldn’t stop listening. Try Stop listening again.',
  'MI-CAP-006': 'Listening was canceled. You can start again when you’re ready.',
  'MI-CAP-007': 'Your meeting audio ended. Rejoin the call, then start listening again.',
  'MI-CAP-008': 'Your browser doesn’t support meeting audio capture. Try an up-to-date version of Chrome or Brave.',
  'MI-CAP-009': 'Listening stopped because an unexpected media source was detected. Refresh the meeting tab and try again.',
  'MI-CON-001': 'We couldn’t connect to Meeting Intelligence. Try again in a moment. If this continues, ask your administrator to check the connection.',
  'MI-CON-002': 'The connection was interrupted. Check your connection, then start listening again.',
  'MI-MIC-001': 'Microphone access is blocked. Choose Enable microphone and allow access in your browser.',
  'MI-MIC-002': 'We couldn’t open your microphone. Check your input device and microphone access in your browser and system settings.',
  'MI-MIC-003': 'No microphone was found. Connect one and try again.',
  'MI-MIC-004': 'Your microphone disconnected. We’re reconnecting it; meeting audio and existing searches can continue.',
  'MI-MIC-005': 'Your microphone is briefly paused. Waiting for it to resume.',
  'MI-MIC-006': 'We’re still reconnecting your microphone. Meeting audio and existing searches can continue.',
  'MI-MIC-007': 'Audio is briefly paused. We’re trying to resume it.',
  'MI-MIC-008': 'No audio is coming through. If you’re speaking, check your selected microphone.',
  'MI-MIC-009': 'Google Meet can’t use your microphone. Open its microphone warning and check your input device and permissions.',
  'MI-MIC-010': 'We couldn’t open the microphone permission request. Try Enable microphone again.',
  'MI-DRV-001': 'We couldn’t check your Google Drive connection. Try again from Settings.',
  'MI-DRV-002': 'We couldn’t connect Google Drive. Try signing in again from Settings.',
  'MI-DRV-003': 'Google Drive sign-in wasn’t completed. You can try again from Settings.',
  'MI-ATL-001': 'We couldn’t check your Jira and Confluence connection. Try again from Settings.',
  'MI-ATL-002': 'We couldn’t connect Jira and Confluence. Try signing in again from Settings.',
  'MI-ATL-004': 'Jira and Confluence aren’t ready to connect. Ask your administrator to finish setup.',
  'MI-ATL-003': 'Jira and Confluence sign-in is still waiting. Finish in the sign-in tab, then try again.',
  'MI-JIR-001': 'We couldn’t load your Jira projects. Try reopening the project list.',
  'MI-JIR-002': 'No Jira projects are available. Ask your Jira administrator for access to a project.',
  'MI-JIR-003': 'We couldn’t create this task. Check your project’s required fields and try again.',
  'MI-JIR-004': 'We couldn’t confirm whether your task was created. Check Jira before retrying to avoid a duplicate.',
  'MI-JIR-005': 'We couldn’t verify the task link. Check your Jira project before retrying.',
  'MI-AUTH-001': 'Your connection needs to be renewed. Sign in again from Settings.',
  'MI-AUTH-002': 'You don’t have access to this action. Check your account permissions or ask your administrator.',
  'MI-CON-003': 'There have been too many requests. Wait a moment, then try again.',
} as const;

export type MessageCode = keyof typeof userMessages;

export function problem(code: MessageCode): string {
  return `${userMessages[code]} (Code: ${code})`;
}

// Render only catalog text, even when an upstream error includes a code. This
// boundary also guarantees a safe, coded fallback for future uncatalogued errors.
export function userError(error: unknown, fallback: MessageCode = 'MI-APP-001'): string {
  const text = error instanceof Error ? error.message : error;
  if (typeof text === 'string') {
    for (const code of Object.keys(userMessages) as MessageCode[]) {
      if (text === problem(code)) return text;
    }
  }
  return problem(fallback);
}

export function httpProblem(status: number, fallback: MessageCode): string {
  if (status === 401) return problem('MI-AUTH-001');
  if (status === 403) return problem('MI-AUTH-002');
  if (status === 429) return problem('MI-CON-003');
  return problem(fallback);
}

export function microphoneProblem(name?: string): string {
  if (name === 'NotAllowedError' || name === 'SecurityError') return problem('MI-MIC-001');
  if (name === 'NotFoundError') return problem('MI-MIC-003');
  return problem('MI-MIC-002');
}
