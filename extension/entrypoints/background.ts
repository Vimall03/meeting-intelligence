import { createBackgroundAuth, backendFetch } from '../lib/background-auth';
import { BACKEND_HTTP_BASE_URL } from '../lib/config';
import { problem, userError, httpProblem } from '../lib/user-messages';
import type { ExtensionMessage, ExtensionState } from '../lib/types';
import { didMeetingEnd, isTimelineForMeeting, meetingIdFromUrl } from '../lib/meeting-session';
import { MICROPHONE_PERMISSION_KEY } from '../lib/microphone-permission';
import { probeWithContentScriptRecovery } from '../lib/content-script-recovery';
import {
  GOOGLE_DRIVE_AUTH_RESULT_KEY,
  GOOGLE_DRIVE_AUTH_TAB_KEY,
  GOOGLE_DRIVE_STATUS_URL,
  isGoogleDriveCallbackUrl,
} from '../lib/config';

import { parseMomSettings, momProblem, updateMomMeeting, terminalMomStates } from '../lib/mom';
import type { MomSettings } from '../lib/mom';

const OFFSCREEN_URL = 'offscreen-page.html';
const meetingStatuses = new Map<number, ExtensionState['meetingStatus']>();
const meetingIds = new Map<number, string>();
const processingDriveOAuthTabs = new Set<number>();
let state: ExtensionState = {
  meetingStatus: 'unknown',
  captureStatus: 'idle',
  backendConnected: false,
};
let offscreenCreation: Promise<void> | undefined;
let captureOperation: Promise<unknown> = Promise.resolve();
let captureCancellation = 0;
let pendingCaptureTabId: number | undefined;
let initialization: Promise<void> | undefined;
let broadcastOperation: Promise<void> = Promise.resolve();
let authEpoch = 0;
let auth: ReturnType<typeof createBackgroundAuth>;
function initializeAuth() { return createBackgroundAuth(async () => {
  authEpoch++;
  captureCancellation++;
  await initializeState();
  // Sign-in refresh can invoke this callback inside a queued start. Do not
  // queue behind that same start: revoked access must interrupt it now.
  await stopCapture();
  state = { ...state, timeline: undefined, timelineOwner: undefined, captureSessionId: undefined, momSettings: undefined, momUserId: undefined, minutes: [], error: undefined };
  await broadcast();
  await chrome.runtime.sendMessage({ type: 'AUTH_CHANGED' }).catch(() => undefined);
}); }

function trusted(sender: chrome.runtime.MessageSender) {
  if (sender.id !== chrome.runtime.id || !sender.url) return false;
  const url = new URL(sender.url);
  return url.protocol === 'chrome-extension:' && url.hostname === chrome.runtime.id;
}


function initializeState() {
  if (!initialization) {
    const getSession = chrome.storage.session.get.bind(chrome.storage.session) as unknown as (key: string) => Promise<{ state?: ExtensionState }>;
    initialization = getSession('state').then(async ({ state: savedState }) => {
      if (savedState) state = savedState;
      const principal = await auth.publicState();
      if (!principal.signedIn || state.momUserId !== principal.userId) {
        state = { ...state, minutes: [], momSettings: undefined, momUserId: undefined };
      }
      // A stopped capture error describes a previous offscreen document. Do
      // not keep presenting it after an extension reload as if it just happened.
      if (state.captureStatus === 'error') {
        state = {
          ...state,
          captureStatus: 'idle',
          captureTabId: undefined,
          captureSessionId: undefined,
          backendConnected: false,
          error: undefined,
        };
        await chrome.storage.session.set({ state });
      }
      // Older builds stored an unscoped timeline. It cannot be safely shown
      // in a later meeting after the service worker restarts.
      if (state.timeline && !state.timelineOwner) state = { ...state, timeline: undefined };
      const active = state.captureStatus === 'capturing' || state.captureStatus === 'connecting';
      // Migrate sessions persisted by builds that used meetingTabId for both
      // the current Meet tab and the tab actually being captured.
      if (active && state.captureTabId === undefined && state.meetingTabId !== undefined) {
        state = { ...state, captureTabId: state.meetingTabId };
      }
      let captureStillExists = false;
      if (active) {
        try {
          const getContexts = chrome.runtime.getContexts as unknown as (filter: unknown) => Promise<unknown[]>;
          const contexts = await getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] });
          if (contexts.length > 0) {
            const result = await chrome.runtime.sendMessage({ type: 'GET_OFFSCREEN_STATUS' }) as { active?: boolean } | undefined;
            captureStillExists = result?.active === true;
          }
        } catch { /* A missing audio page means the old session has ended. */ }
      }
      if (active && !captureStillExists) {
        state = { ...state, captureStatus: 'idle', captureTabId: undefined, backendConnected: false, error: undefined };
        await chrome.storage.session.set({ state });
      }
    });
  }
  return initialization;
}

async function ensureOffscreen() {
  const getContexts = chrome.runtime.getContexts as unknown as (filter: unknown) => Promise<unknown[]>;
  const contexts = await getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] });
  if (!contexts.length) {
    if (!offscreenCreation) offscreenCreation = chrome.offscreen.createDocument({
      url: OFFSCREEN_URL,
      reasons: [chrome.offscreen.Reason.USER_MEDIA],
      justification: 'Capture only the authorized meeting tab audio and microphone.',
    }).finally(() => { offscreenCreation = undefined; });
    await offscreenCreation;
  }
}

function broadcast() {
  state = { ...state, revision: (state.revision ?? 0) + 1 };
  const snapshot = state;
  // Storage writes must finish in revision order. Concurrent meeting, audio,
  // and timeline updates otherwise can leave an older snapshot persisted.
  broadcastOperation = broadcastOperation.catch(() => undefined).then(async () => {
    await chrome.storage.session.set({ state: snapshot });
    chrome.runtime.sendMessage({ type: 'STATE', state: snapshot }).catch(() => undefined);
  });
  return broadcastOperation;
}

function recordMeetingStatus(
  tabId: number,
  status: ExtensionState['meetingStatus'],
  meetingId?: string,
): boolean {
  meetingStatuses.set(tabId, status);
  if (status === 'in-meeting' && meetingId) meetingIds.set(tabId, meetingId);
  else meetingIds.delete(tabId);
  const ended = didMeetingEnd(state.timelineOwner, tabId, status, meetingId);
  if (ended) state = { ...state, timeline: undefined, timelineOwner: undefined, captureSessionId: undefined };
  return ended;
}

async function checkMeetingTab(tabId: number) {
  let connected = false;
  try {
    const tab = await chrome.tabs.get(tabId);
    const response = await probeWithContentScriptRecovery({
      probe: () => chrome.tabs.sendMessage(tabId, { type: 'GET_MEETING_STATUS' }) as Promise<{
        status?: ExtensionState['meetingStatus']; meetingId?: string;
      }>,
      inject: async (files) => {
        await chrome.scripting.executeScript({ target: { tabId }, files });
      },
      manifest: chrome.runtime.getManifest(),
      tabUrl: tab.url,
    }) as {
      status?: ExtensionState['meetingStatus']; meetingId?: string;
    };
    connected = true;
    if (response?.status) recordMeetingStatus(tabId, response.status, response.meetingId);
  } catch { /* The caller receives the active-Meet error below. */ }
  return { connected, inMeeting: connected && meetingStatuses.get(tabId) === 'in-meeting' };
}

async function momRequest(path: string, method = 'GET', body?: string) {
  const epoch = authEpoch;
  const principal = await auth.publicState();
  if (!principal.signedIn || !principal.userId) throw new Error(problem('MI-AUTH-105'));
  const result = await backendFetch(auth, `${BACKEND_HTTP_BASE_URL}/api/mom/${path}`, method, body);
  if (epoch !== authEpoch || principal.userId !== (await auth.publicState()).userId) throw new Error(problem('MI-AUTH-105'));
  if (result.status !== 200) throw new Error(result.status === 401 ? problem('MI-AUTH-105') : momProblem(result.data));
  return { data: result.data, userId: principal.userId };
}

async function loadMomSettings(mode?: string, timezone?: string): Promise<MomSettings> {
  const result = await momRequest('settings', mode ? 'POST' : 'GET', mode ? JSON.stringify({ mode, timezone: timezone ?? 'UTC' }) : undefined);
  const prefs = parseMomSettings(result.data);
  if (state.momUserId !== result.userId) state = { ...state, minutes: [] };
  state = { ...state, momSettings: prefs, momUserId: result.userId };
  await broadcast();
  return prefs;
}

async function prepareCapture(tabId: number) {
  await initializeState();
  if (state.captureStatus === 'capturing' || state.captureStatus === 'connecting') {
    return { ok: false, error: problem('MI-CAP-002') };
  }
  if (!(await checkMeetingTab(tabId)).inMeeting) {
    state = { ...state, captureStatus: 'error', error: problem('MI-CAP-001') };
    await broadcast();
    return { ok: false, error: state.error };
  }
  // Refresh sign-in and load the audio page before Start is clicked. A tab
  // stream ID expires after a few seconds and must never wait on this work.
  await Promise.all([auth.access(), ensureOffscreen(), loadMomSettings()]);
  state = { ...state, meetingStatus: 'in-meeting', meetingTabId: tabId, captureStatus: 'idle', error: undefined };
  await broadcast();
  return { ok: true };
}

function startCapture(tabId: number) {
  const epoch = authEpoch;
  const cancellation = captureCancellation;
  return enqueue(async () => {
    try {
      await initializeState();
      if (epoch !== authEpoch) throw new Error(problem('MI-AUTH-105'));
      if (cancellation !== captureCancellation) return { ok: false, error: problem('MI-CAP-006'), state };
      if (state.captureStatus === 'capturing' || state.captureStatus === 'connecting') {
        return { ok: false, error: problem('MI-CAP-002'), state };
      }
      pendingCaptureTabId = tabId;
      const [, , meeting, prefs] = await Promise.all([auth.access(), ensureOffscreen(), checkMeetingTab(tabId), loadMomSettings()]);
      if (epoch !== authEpoch) throw new Error(problem('MI-AUTH-105'));
      if (cancellation !== captureCancellation) return { ok: false, error: problem('MI-CAP-006'), state };
      if (!meeting.inMeeting) throw new Error(problem('MI-CAP-001'));
      const meetingId = meetingIds.get(tabId);
      if (!meetingId) throw new Error(problem('MI-CAP-004'));
      const tab = await chrome.tabs.get(tabId);
      if (meetingIdFromUrl(tab.url) !== meetingId) throw new Error(problem('MI-CAP-001'));
      if (cancellation !== captureCancellation) return { ok: false, error: problem('MI-CAP-006'), state };

      // Toolbar invocation grants activeTab for this tab until navigation. Its
      // grant survives awaited setup, but the single-use stream ID does not.
      // Mint the ID last and hand it to the audio page immediately.
      const streamId = await new Promise<string>((resolve, reject) => {
        chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (id) => {
          const error = chrome.runtime.lastError;
          if (error) reject(new Error(error.message));
          else if (id) resolve(id);
          else reject(new Error(problem('MI-CAP-003')));
        });
      });
      const captureSessionId = crypto.randomUUID();
      state = { ...state, meetingStatus: 'in-meeting', meetingTabId: tabId, captureTabId: tabId, captureSessionId, captureMode: prefs.mode, captureStatus: 'connecting', backendConnected: false, timeline: undefined, audioWarning: undefined, timelineOwner: { tabId, meetingId }, error: undefined };
      void broadcast();
      if (epoch !== authEpoch) throw new Error(problem('MI-AUTH-105'));
      if (cancellation !== captureCancellation) return { ok: false, error: problem('MI-CAP-006'), state };
      const result = await chrome.runtime.sendMessage({ type: 'START_OFFSCREEN', streamId, captureSessionId, roomId: meetingId, userId: state.momUserId, mode: prefs.mode }) as { ok?: boolean; error?: string } | undefined;
      if (!result?.ok) {
        if (state.captureStatus === 'idle') return { ok: false, error: problem('MI-CAP-006'), state };
        if (state.captureStatus !== 'error') {
          state = { ...state, captureStatus: 'error', error: result?.error ?? problem('MI-CAP-004') };
          await broadcast();
        }
        return { ok: false, error: state.error, state };
      }
      return { ok: true, state };
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : problem('MI-CAP-004');
      const needsInvocation = /not been invoked|activeTab permission|permission for the specified tab/i.test(message);
      state = {
        ...state,
        captureStatus: 'error',
        error: needsInvocation
          ? problem('MI-CAP-003')
          : userError(error, 'MI-CAP-004'),
      };
      await broadcast();
      return { ok: false, error: state.error, state };
    } finally {
      pendingCaptureTabId = undefined;
    }
  });
}

async function handleMessage(message: ExtensionMessage, sender: chrome.runtime.MessageSender): Promise<unknown> {
  if (message.type.startsWith('AUTH_') || message.type === 'MEETING_TICKET') {
    if (!trusted(sender)) return { ok: false, error: problem('MI-AUTH-106') };
    if (message.type === 'AUTH_STATE') return { ok: true, session: await auth.publicState() };
    if (message.type === 'AUTH_REQUEST') return { ok: true, ...await auth.requestCode(message.email) };
    if (message.type === 'AUTH_VERIFY') { const session = await auth.verifyCode(message.code); await chrome.runtime.sendMessage({ type: 'AUTH_CHANGED' }).catch(() => undefined); return { ok: true, session }; }
    if (message.type === 'AUTH_LOGOUT') { await auth.logout(); return { ok: true }; }
    if (message.type === 'AUTH_FETCH') return { ok: true, ...await backendFetch(auth, message.url, message.method, message.body) };
    if (message.type === 'MEETING_TICKET') {
      const result = await backendFetch(auth, `${BACKEND_HTTP_BASE_URL}/api/meeting/connection-ticket`, 'POST', '{}');
      if (result.status !== 200) throw new Error(problem('MI-AUTH-104'));
      return { ok: true, ticket: result.data.ticket };
    }
    return { ok: false };
  }
  if (message.type.startsWith('MOM_')) {
    if (!trusted(sender)) return { ok: false, error: problem('MI-AUTH-106') };
    await initializeState();
    if (message.type === 'MOM_SETTINGS') return { ok: true, settings: await loadMomSettings(message.mode, message.timezone) };
    if (message.type === 'MOM_UPDATE') {
      if (!sender.url?.endsWith('/offscreen-page.html') || message.userId !== (await auth.publicState()).userId) return { ok: false };
      const known = state.minutes?.find(item => item.sessionId === message.meeting.sessionId);
      if (known && terminalMomStates.includes(known.state)) return { ok: true };
      if (known && known.captureId === state.captureSessionId && message.meeting.captureId !== known.captureId) return { ok: true };
      if (known && known.captureId === message.meeting.captureId && known.state !== 'active' && message.meeting.state === 'active') return { ok: true };
      state = { ...state, momUserId: message.userId, minutes: updateMomMeeting(state.minutes ?? [], message.meeting) };
      await broadcast();
      return { ok: true };
    }
    if (message.type === 'MOM_STATUS' || message.type === 'MOM_FINISH' || message.type === 'MOM_RETRY') {
      if (!/^[a-f0-9]{32}$/.test(message.sessionId)) throw new Error(problem('MI-MOM-005'));
      const suffix = message.type === 'MOM_FINISH' ? '/finish' : message.type === 'MOM_RETRY' ? '/retry' : '';
      const result = await momRequest(`sessions/${message.sessionId}${suffix}`, suffix ? 'POST' : 'GET');
      const data = result.data;
      const prior = state.minutes?.find(item => item.sessionId === message.sessionId);
      if (prior) state = { ...state, minutes: updateMomMeeting(state.minutes ?? [], { ...prior, state: data.state, finalizeAfter: data.finalize_after ?? undefined, supportCode: data.support_code }) };
      await broadcast();
      return { ok: true, status: data };
    }
  }
  if (message.type === 'START_CAPTURE') return startCapture(sender.tab?.id ?? message.tabId);
  if (message.type === 'MIC_PERMISSION_RESULT') {
    if (message.granted || message.permissionState === 'denied') {
      await chrome.storage.local.set({ [MICROPHONE_PERMISSION_KEY]: message.granted });
    }
    if (message.granted) {
      await initializeState();
      if (state.captureStatus === 'error' && /microphone/i.test(state.error ?? '')) {
        state = { ...state, captureStatus: 'idle', error: undefined };
        await broadcast();
      }
    }
    const permissionTabId = sender.tab?.id;
    if (message.granted && permissionTabId !== undefined) {
      // Let the result reach the open side panel before returning focus to the meeting.
      setTimeout(() => { void chrome.tabs.remove(permissionTabId).catch(() => undefined); }, 600);
    }
    return { ok: true };
  }
  if (message.type === 'MICROPHONE_PERMISSION_STATUS') {
    const saved = await chrome.storage.local.get(MICROPHONE_PERMISSION_KEY);
    if (saved[MICROPHONE_PERMISSION_KEY] !== message.granted) {
      await chrome.storage.local.set({ [MICROPHONE_PERMISSION_KEY]: message.granted });
    }
    return { ok: true };
  }
  if (message.type === 'CLOSE_MIC_PERMISSION_TAB') {
    const permissionTabId = sender.tab?.id;
    if (permissionTabId !== undefined) {
      setTimeout(() => { void chrome.tabs.remove(permissionTabId).catch(() => undefined); }, 50);
    }
    return { ok: true };
  }
  await initializeState();
  if (message.type === 'MEETING_STATUS') {
    const tabId = sender.tab?.id ?? message.tabId;
    const ended = recordMeetingStatus(tabId, message.status, message.meetingId);
    if (state.captureStatus === 'idle' || state.meetingTabId === tabId) {
      state = { ...state, meetingStatus: message.status, meetingTabId: tabId };
    }
    if ((message.status !== 'in-meeting' || ended)
        && (pendingCaptureTabId === tabId || (state.captureTabId === tabId && state.captureStatus !== 'idle'))) await requestStopCapture();
    else await broadcast();
    return { ok: true };
  } else if (message.type === 'GET_STATE') {
    const tabId = sender.tab?.id ?? message.tabId;
    return tabId === undefined ? state : {
      ...state,
      meetingStatus: meetingStatuses.get(tabId) ?? 'unknown',
      meetingTabId: tabId,
      timeline: isTimelineForMeeting(state.timelineOwner, tabId, meetingIds.get(tabId), meetingStatuses.get(tabId) ?? 'unknown') ? state.timeline : undefined,
    };
  } else if (message.type === 'CHECK_MEETING_TAB') {
    const result = await checkMeetingTab(message.tabId);
    return {
      status: meetingStatuses.get(message.tabId) ?? 'unknown',
      meetingId: meetingIds.get(message.tabId),
      contentScriptMissing: !result.connected,
    };
  } else if (message.type === 'PREPARE_CAPTURE') {
    return enqueue(() => prepareCapture(sender.tab?.id ?? message.tabId));
  } else if (message.type === 'STOP_CAPTURE') {
    await requestStopCapture(message.finishMinutes);
    return { ok: true, state };
  } else if (message.type === 'CAPTURE_STATE') {
    if (!trusted(sender) || !sender.url?.endsWith('/offscreen-page.html')) return { ok: false };
    if (!state.captureSessionId || message.captureSessionId !== state.captureSessionId) return { ok: true };
    const incoming = message.state.timeline;
    const current = state.timeline;
    const staleTimeline = incoming && current
      && incoming.session_id === current.session_id
      && incoming.revision < current.revision;
    const acceptTimeline = incoming && state.timelineOwner && (state.captureStatus === 'connecting' || state.captureStatus === 'capturing');
    state = { ...state, ...message.state, timeline: acceptTimeline && !staleTimeline ? incoming : current };
    await broadcast();
    return { ok: true };
  }
  return { ok: false, error: problem('MI-APP-001') };
}

chrome.runtime.onMessage.addListener((message: ExtensionMessage, sender, sendResponse) => {
  if (message.type === 'START_OFFSCREEN' || message.type === 'STOP_OFFSCREEN' || message.type === 'GET_OFFSCREEN_STATUS') return false;
  void handleMessage(message, sender)
    .then((response) => sendResponse(response))
    .catch((error: unknown) => sendResponse({ ok: false, error: userError(error) }));
  return true;
});

async function stopCapture(finishMinutes = false) {
  const result = await chrome.runtime.sendMessage({ type: 'STOP_OFFSCREEN', finishMinutes }).catch(() => undefined) as { ok?: boolean; warning?: string } | undefined;
  if (result && !result.ok) throw new Error(problem('MI-CAP-005'));
  state = { ...state, captureStatus: 'idle', captureTabId: undefined, captureSessionId: undefined, backendConnected: false, error: result?.warning ? userError(result.warning, 'MI-MOM-003') : undefined };
  await broadcast();
}

function enqueue<T>(operation: () => Promise<T>): Promise<T> {
  const next = captureOperation.then(operation, operation);
  captureOperation = next.then(() => undefined, () => undefined);
  return next;
}

function requestStopCapture(finishMinutes = false) {
  captureCancellation++;
  return enqueue(() => stopCapture(finishMinutes));
}

async function handleTabRemoved(tabId: number) {
  await initializeState();
  meetingStatuses.delete(tabId);
  meetingIds.delete(tabId);
  if (state.timelineOwner?.tabId === tabId) {
    state = { ...state, timeline: undefined, timelineOwner: undefined, captureSessionId: undefined };
    void broadcast();
  }
  if (pendingCaptureTabId === tabId || (state.captureTabId === tabId && state.captureStatus !== 'idle')) void requestStopCapture();
  void handleGoogleDriveOAuthTabClosed(tabId);
}

chrome.tabs.onRemoved.addListener((tabId) => {
  void handleTabRemoved(tabId).catch(() => undefined);
});

async function handleTabUpdated(tabId: number, change: chrome.tabs.TabChangeInfo) {
  await initializeState();
  if (!change.url) return;
  if (pendingCaptureTabId === tabId && meetingIdFromUrl(change.url) !== meetingIds.get(tabId)) {
    void requestStopCapture();
  }
  if (state.timelineOwner?.tabId === tabId && meetingIdFromUrl(change.url) !== state.timelineOwner.meetingId) {
    state = { ...state, timeline: undefined, timelineOwner: undefined, captureSessionId: undefined };
    meetingIds.delete(tabId);
    void broadcast();
    if (state.captureTabId === tabId && state.captureStatus !== 'idle') void requestStopCapture();
  }
}

chrome.tabs.onUpdated.addListener((tabId, change) => {
  void handleTabUpdated(tabId, change).catch(() => undefined);
});

async function backgroundAuthenticatedFetch(url: string, _options?: unknown) {
  const result = await backendFetch(auth, url);
  return new Response(JSON.stringify(result.data), { status: result.status });
}

async function handleGoogleDriveOAuthNavigation(tabId: number, url: string) {
  if (processingDriveOAuthTabs.has(tabId)) return;
  processingDriveOAuthTabs.add(tabId);
  try {
    const stored = await chrome.storage.local.get(GOOGLE_DRIVE_AUTH_TAB_KEY);
    const pending = stored[GOOGLE_DRIVE_AUTH_TAB_KEY] as { tabId?: number; redirectUri?: string; userId?: string } | undefined;
    if (pending && pending.userId !== (await auth.publicState()).userId) return;
    if (pending?.tabId !== tabId || !pending.redirectUri || !isGoogleDriveCallbackUrl(url, pending.redirectUri)) return;

    const callback = new URL(url);
    const oauthError = callback.searchParams.get('error');
    if (oauthError) {
      await chrome.storage.local.set({
        [GOOGLE_DRIVE_AUTH_RESULT_KEY]: {
          connected: false,
          error: problem('MI-DRV-003'),
          completedAt: Date.now(),
        },
      });
      await chrome.storage.local.remove(GOOGLE_DRIVE_AUTH_TAB_KEY);
      return;
    }

    const response = await backgroundAuthenticatedFetch(GOOGLE_DRIVE_STATUS_URL, { cache: 'no-store' });
    if (!response.ok) throw new Error(httpProblem(response.status, 'MI-DRV-001'));
    const status = await response.json() as { connected?: boolean };
    if (!status.connected) throw new Error(problem('MI-DRV-002'));

    if (pending.userId !== (await auth.publicState()).userId) return;
    await chrome.storage.local.set({
      [GOOGLE_DRIVE_AUTH_RESULT_KEY]: { connected: true, userId: pending.userId, completedAt: Date.now() },
    });
    await chrome.storage.local.remove(GOOGLE_DRIVE_AUTH_TAB_KEY);
    await chrome.tabs.remove(tabId).catch(() => undefined);
  } catch (error) {
    try {
      const latest = await chrome.storage.local.get(GOOGLE_DRIVE_AUTH_TAB_KEY);
      const pending = latest[GOOGLE_DRIVE_AUTH_TAB_KEY] as { tabId?: number; userId?: string } | undefined;
      // Only clean up this attempt. A newer sign-in may have replaced it while
      // the callback status request was in flight.
      if (pending?.tabId === tabId) {
        await chrome.storage.local.remove(GOOGLE_DRIVE_AUTH_TAB_KEY);
        await chrome.storage.local.set({
          [GOOGLE_DRIVE_AUTH_RESULT_KEY]: {
            connected: false,
            error: userError(error, 'MI-DRV-002'),
            completedAt: Date.now(),
          },
        });
      }
    } catch {
      // Authorization UI failures should not disrupt meeting capture.
    }
  } finally {
    processingDriveOAuthTabs.delete(tabId);
  }
}

async function handleGoogleDriveOAuthTabClosed(tabId: number) {
  try {
    const stored = await chrome.storage.local.get(GOOGLE_DRIVE_AUTH_TAB_KEY);
    const pending = stored[GOOGLE_DRIVE_AUTH_TAB_KEY] as { tabId?: number; userId?: string } | undefined;
    if (pending && pending.userId !== (await auth.publicState()).userId) return;
    if (pending?.tabId !== tabId) return;
    await chrome.storage.local.remove(GOOGLE_DRIVE_AUTH_TAB_KEY);
    await chrome.storage.local.set({
      [GOOGLE_DRIVE_AUTH_RESULT_KEY]: {
        connected: false,
        error: problem('MI-DRV-003'),
        completedAt: Date.now(),
      },
    });
  } catch {
    // A tab closing should never disrupt meeting capture or other extension state.
  }
}

chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
  if (change.status === 'complete' && tab.url) {
    void handleGoogleDriveOAuthNavigation(tabId, tab.url);
  }
});

export default defineBackground(() => {
  auth = initializeAuth();
  // The toolbar invocation grants capture access to the active tab. Keep the
  // action handler responsible for opening the panel as well.
  chrome.sidePanel?.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => undefined);
  chrome.action.onClicked.addListener((tab) => {
    if (tab.id === undefined) return;
    // Keep this call directly in the action handler: sidePanel.open also
    // requires the click gesture and must not follow awaited work.
    chrome.sidePanel.open({ tabId: tab.id }).catch(() => undefined);
  });
});
