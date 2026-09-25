import type { ExtensionMessage, ExtensionState } from '../lib/types';
import { MICROPHONE_PERMISSION_KEY } from '../lib/microphone-permission';

type SidePanelLifecycle = {
  onOpened?: { addListener: (listener: (info: { tabId?: number; windowId: number }) => void) => void };
  onClosed?: { addListener: (listener: (info: { tabId?: number; windowId: number }) => void) => void };
};

const OFFSCREEN_URL = 'offscreen-page.html';
const meetingStatuses = new Map<number, ExtensionState['meetingStatus']>();
let state: ExtensionState = {
  meetingStatus: 'unknown',
  captureStatus: 'idle',
  backendConnected: false,
};
let offscreenCreation: Promise<void> | undefined;
let captureOperation: Promise<void> = Promise.resolve();
let initialization: Promise<void> | undefined;

function initializeState() {
  if (!initialization) {
    const getSession = chrome.storage.session.get.bind(chrome.storage.session) as unknown as (key: string) => Promise<{ state?: ExtensionState }>;
    initialization = getSession('state').then(async ({ state: savedState }) => {
      if (savedState) state = savedState;
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
      justification: 'Capture only the authorized Meet tab audio and microphone.',
    }).finally(() => { offscreenCreation = undefined; });
    await offscreenCreation;
  }
}

async function broadcast() {
  state = { ...state, revision: (state.revision ?? 0) + 1 };
  const snapshot = state;
  await chrome.storage.session.set({ state: snapshot });
  // Keep extension pages and Meet controls live while the side panel remains open.
  chrome.runtime.sendMessage({ type: 'STATE', state: snapshot }).catch(() => undefined);
  const tabs = await chrome.tabs.query({});
  await Promise.all(tabs.map((tab) => tab.id ? chrome.tabs.sendMessage(tab.id, { type: 'STATE', state: snapshot, tabId: tab.id }).catch(() => undefined) : undefined));
}

async function checkMeetingTab(tabId: number) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, { type: 'GET_MEETING_STATUS' }) as { status?: ExtensionState['meetingStatus'] };
    if (response?.status) meetingStatuses.set(tabId, response.status);
  } catch { /* The caller receives the active-Meet error below. */ }
  return meetingStatuses.get(tabId) === 'in-meeting';
}

async function prepareCapture(tabId: number) {
  await initializeState();
  if (!await checkMeetingTab(tabId)) {
    state = { ...state, captureStatus: 'error', error: 'Open an active Google Meet call first.' };
    await broadcast();
    return { ok: false, error: state.error };
  }
  if (state.captureStatus === 'capturing' || state.captureStatus === 'connecting') {
    return { ok: false, error: 'A capture is already running. Stop it before starting another.' };
  }
  await ensureOffscreen();
  state = { ...state, meetingStatus: 'in-meeting', meetingTabId: tabId, captureStatus: 'idle', error: undefined };
  await broadcast();
  return { ok: true };
}

function startCapture(tabId: number) {
  if (state.captureStatus === 'capturing' || state.captureStatus === 'connecting') {
    return Promise.resolve({ ok: false, error: 'A capture is already running.' });
  }
  const restoredState = initializeState();
  // Call tabCapture immediately from the click message. A side-panel click
  // alone does not grant activeTab; the explicit action click below does.
  const streamIdRequest = new Promise<string>((resolve, reject) => {
    chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (streamId) => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else if (streamId) resolve(streamId);
      else reject(new Error('Brave did not grant this Meet tab for audio capture.'));
    });
  });
  return Promise.all([restoredState, streamIdRequest]).then(async ([, streamId]) => {
    if (state.captureStatus === 'capturing' || state.captureStatus === 'connecting') {
      return { ok: false, error: 'A capture is already running.', state };
    }
    await ensureOffscreen();
    state = { ...state, meetingStatus: 'in-meeting', meetingTabId: tabId, captureTabId: tabId, captureStatus: 'connecting', backendConnected: false, error: undefined };
    void broadcast();
    const result = await chrome.runtime.sendMessage({ type: 'START_OFFSCREEN', streamId }) as { ok?: boolean; error?: string } | undefined;
    if (!result?.ok) {
      if (state.captureStatus === 'idle') return { ok: false, error: 'Capture was canceled.', state };
      if (state.captureStatus !== 'error') {
        state = { ...state, captureStatus: 'error', error: result?.error ?? 'The audio capture could not be started. Try again.' };
        await broadcast();
      }
      return { ok: false, error: state.error, state };
    }
    return { ok: true, state };
  }).catch(async (error: unknown) => {
    const message = error instanceof Error ? error.message : 'Meet audio capture failed.';
    const needsInvocation = /not been invoked|activeTab permission|permission for the specified tab/i.test(message);
    state = {
      ...state,
      captureStatus: 'error',
      error: needsInvocation
        ? 'To authorize audio-only capture, click the Meeting Intelligence toolbar icon while this Meet tab is active, then press Start capture here.'
        : message,
      tabAudioAuthorizedTabIds: needsInvocation ? state.tabAudioAuthorizedTabIds?.filter((id) => id !== tabId) : state.tabAudioAuthorizedTabIds,
    };
    await broadcast();
    return { ok: false, error: state.error, state };
  });
}

async function handleMessage(message: ExtensionMessage, sender: chrome.runtime.MessageSender): Promise<unknown> {
  if (message.type === 'START_CAPTURE') return startCapture(sender.tab?.id ?? message.tabId);
  // Call sidePanel.open before any awaited storage work so the request remains
  // attached to the click gesture from the Meet launcher.
  if (message.type === 'OPEN_SIDE_PANEL') {
    const tabId = sender.tab?.id ?? message.tabId;
    if (!chrome.sidePanel?.open) throw new Error('This browser does not support the extension side panel.');
    await chrome.sidePanel.open({ tabId });
    return { ok: true };
  }
  if (message.type === 'MIC_PERMISSION_RESULT') {
    await chrome.storage.local.set({ [MICROPHONE_PERMISSION_KEY]: message.granted });
    if (message.granted) {
      await initializeState();
      if (state.captureStatus === 'error' && /microphone/i.test(state.error ?? '')) {
        state = { ...state, captureStatus: 'idle', error: undefined };
        await broadcast();
      }
    }
    const permissionTabId = sender.tab?.id;
    if (message.granted && permissionTabId !== undefined) {
      // Let the result reach the open side panel before returning focus to Meet.
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
    if (tabId !== undefined) meetingStatuses.set(tabId, message.status);
    if (state.captureStatus === 'idle' || state.meetingTabId === tabId) {
      state = { ...state, meetingStatus: message.status, meetingTabId: tabId };
    }
    if (message.status !== 'in-meeting' && state.captureTabId === tabId && state.captureStatus !== 'idle') await enqueue(stopCapture);
    else await broadcast();
    return { ok: true };
  } else if (message.type === 'GET_STATE') {
    const tabId = sender.tab?.id ?? message.tabId;
    return tabId === undefined ? state : {
      ...state,
      meetingStatus: meetingStatuses.get(tabId) ?? 'unknown',
      meetingTabId: tabId,
    };
  } else if (message.type === 'PREPARE_CAPTURE') {
    return prepareCapture(sender.tab?.id ?? message.tabId);
  } else if (message.type === 'STOP_CAPTURE') {
    await enqueue(stopCapture);
    return { ok: true, state };
  } else if (message.type === 'CAPTURE_STATE') {
    state = { ...state, ...message.state };
    await broadcast();
    return { ok: true };
  }
  return { ok: false, error: 'Unsupported extension message.' };
}

chrome.runtime.onMessage.addListener((message: ExtensionMessage, sender, sendResponse) => {
  if (message.type === 'START_OFFSCREEN' || message.type === 'STOP_OFFSCREEN' || message.type === 'GET_OFFSCREEN_STATUS') return false;
  void handleMessage(message, sender)
    .then((response) => sendResponse(response))
    .catch((error: unknown) => sendResponse({ ok: false, error: error instanceof Error ? error.message : 'Extension request failed.' }));
  return true;
});

async function stopCapture() {
  const result = await chrome.runtime.sendMessage({ type: 'STOP_OFFSCREEN' }).catch(() => undefined) as { ok?: boolean } | undefined;
  if (result && !result.ok) throw new Error('Could not release the audio capture. Try stopping it again.');
  state = { ...state, captureStatus: 'idle', captureTabId: undefined, backendConnected: false, jevDecision: undefined, error: undefined };
  await broadcast();
}

function enqueue(operation: () => Promise<void>) {
  captureOperation = captureOperation.then(operation, operation);
  return captureOperation;
}

chrome.tabs.onRemoved.addListener((tabId) => {
  meetingStatuses.delete(tabId);
  if (state.tabAudioAuthorizedTabIds?.includes(tabId)) {
    state = { ...state, tabAudioAuthorizedTabIds: state.tabAudioAuthorizedTabIds.filter((id) => id !== tabId) };
    void broadcast();
  }
  if (state.captureTabId === tabId && state.captureStatus !== 'idle') void enqueue(stopCapture);
});

chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (!change.url || !state.tabAudioAuthorizedTabIds?.includes(tabId)) return;
  try {
    if (new URL(change.url).hostname === 'meet.google.com') return;
  } catch { /* A non-Meet URL revokes the activeTab grant. */ }
  state = { ...state, tabAudioAuthorizedTabIds: state.tabAudioAuthorizedTabIds.filter((id) => id !== tabId) };
  void broadcast();
});

async function broadcastPanelVisibility(info: { tabId?: number; windowId: number }, visible: boolean) {
  let tabId = info.tabId;
  if (tabId === undefined) {
    const [activeTab] = await chrome.tabs.query({ active: true, windowId: info.windowId });
    tabId = activeTab?.id;
  }
  if (tabId !== undefined) {
    chrome.tabs.sendMessage(tabId, { type: 'PANEL_VISIBILITY', visible, tabId }).catch(() => undefined);
  }
}

const sidePanelLifecycle = chrome.sidePanel as unknown as SidePanelLifecycle | undefined;
sidePanelLifecycle?.onOpened?.addListener((info) => { void broadcastPanelVisibility(info, true); });
sidePanelLifecycle?.onClosed?.addListener((info) => { void broadcastPanelVisibility(info, false); });

export default defineBackground(() => {
  // setPanelBehavior(openPanelOnActionClick) bypasses onClicked in Chromium,
  // so it cannot provide the per-tab grant required by tabCapture.
  chrome.sidePanel?.setPanelBehavior({ openPanelOnActionClick: false }).catch(() => undefined);
  chrome.action.onClicked.addListener((tab) => {
    if (tab.id === undefined) return;
    // Keep this call directly in the action handler: sidePanel.open also
    // requires the click gesture and must not follow awaited work.
    chrome.sidePanel.open({ tabId: tab.id }).catch(() => undefined);
    let isMeet = false;
    try { isMeet = new URL(tab.url ?? '').hostname === 'meet.google.com'; } catch { /* Ignore non-web tabs. */ }
    if (!isMeet) return;
    void initializeState().then(async () => {
      state = { ...state, tabAudioAuthorizedTabIds: [...new Set([...(state.tabAudioAuthorizedTabIds ?? []), tab.id!])] };
      await broadcast();
    });
  });
});
