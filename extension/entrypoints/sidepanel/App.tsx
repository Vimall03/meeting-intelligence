import { useCallback, useEffect, useRef, useState } from 'react';
import type { ExtensionMessage, ExtensionState, MeetingStatus } from '../../lib/types';
import { MICROPHONE_PERMISSION_KEY } from '../../lib/microphone-permission';
import {
  GOOGLE_DRIVE_AUTH_RESULT_KEY,
  GOOGLE_DRIVE_AUTH_START_URL,
  GOOGLE_DRIVE_AUTH_TAB_KEY,
  GOOGLE_DRIVE_STATUS_URL,
} from '../../lib/config';
import './style.css';

type MicrophonePermission = 'checking' | 'prompt' | 'granted' | 'denied' | 'requesting';
type GoogleDriveConnection = 'checking' | 'connected' | 'disconnected' | 'error';
type Notice = { tone: 'error' | 'success' | 'info'; text: string };

async function readGoogleDriveStatus(): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch(GOOGLE_DRIVE_STATUS_URL, { cache: 'no-store' });
  } catch {
    throw new Error('Could not reach the local backend. Make sure it is running, then retry.');
  }
  const payload = await response.json().catch(() => ({})) as {
    connected?: boolean;
    detail?: { message?: string };
  };
  if (!response.ok) {
    throw new Error(payload.detail?.message ?? `Google Drive status failed (${response.status}).`);
  }
  return payload.connected === true;
}

const initialState: ExtensionState = {
  meetingStatus: 'unknown',
  captureStatus: 'idle',
  backendConnected: false,
};

function isMeetTab(tab: chrome.tabs.Tab | undefined) {
  try {
    return new URL(tab?.url ?? '').hostname === 'meet.google.com';
  } catch {
    return false;
  }
}

function googleDriveFileUrl(file: { id: string; web_view_link?: string }) {
  const fallback = `https://drive.google.com/open?id=${encodeURIComponent(file.id)}`;
  try {
    const url = new URL(file.web_view_link || fallback);
    if (url.protocol === 'https:' && (url.hostname === 'drive.google.com' || url.hostname.endsWith('.google.com'))) {
      return url.toString();
    }
  } catch {
    // Fall back to Google's standard file URL for missing or malformed links.
  }
  return fallback;
}

function driveFileType(mimeType: string) {
  const nativeTypes: Record<string, string> = {
    'application/vnd.google-apps.document': 'Google Docs',
    'application/vnd.google-apps.spreadsheet': 'Google Sheets',
    'application/vnd.google-apps.presentation': 'Google Slides',
  };
  if (nativeTypes[mimeType]) return nativeTypes[mimeType];
  if (mimeType === 'application/pdf') return 'PDF';
  if (mimeType.startsWith('image/')) return 'Image';
  if (mimeType.startsWith('video/')) return 'Video';
  if (mimeType.startsWith('audio/')) return 'Audio';
  return mimeType.split('/').at(-1)?.replaceAll('-', ' ') || 'File';
}

async function readMicrophonePermission(): Promise<PermissionState | undefined> {
  try {
    const permission = await navigator.permissions.query({ name: 'microphone' as PermissionName });
    return permission.state;
  } catch {
    // Some browser builds do not expose microphone through the Permissions API.
    return undefined;
  }
}

type SidePanelCloseApi = {
  close?: (options: { tabId?: number; windowId?: number }) => Promise<void>;
};

export default function App() {
  const [state, setState] = useState(initialState);
  const [activeTab, setActiveTab] = useState<chrome.tabs.Tab>();
  const [microphone, setMicrophone] = useState<MicrophonePermission>('checking');
  const [googleDriveConnection, setGoogleDriveConnection] = useState<GoogleDriveConnection>('checking');
  const [googleDriveAuthorizing, setGoogleDriveAuthorizing] = useState(false);
  const [googleDriveBusy, setGoogleDriveBusy] = useState(false);
  const [googleDriveError, setGoogleDriveError] = useState<string>();
  const [notice, setNotice] = useState<Notice>();
  const [busy, setBusy] = useState(false);
  const [readyTabId, setReadyTabId] = useState<number>();
  const activeTabId = useRef<number | undefined>(undefined);
  const microphoneRequestTabId = useRef<number | undefined>(undefined);
  const refreshSequence = useRef(0);
  const contentScriptMissingRef = useRef(false);

  const refresh = useCallback(async () => {
    const sequence = ++refreshSequence.current;
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (sequence !== refreshSequence.current) return;
      if (tab?.url === chrome.runtime.getURL('mic-permission.html')) return;
      const previousTabId = activeTabId.current;
      setActiveTab(tab);
      activeTabId.current = tab?.id ?? undefined;
      if (tab?.id !== undefined && previousTabId !== tab.id) {
        chrome.tabs.sendMessage(tab.id, { type: 'PANEL_VISIBILITY', visible: true, tabId: tab.id }).catch(() => undefined);
      }
      if (!tab?.id) {
        setState(initialState);
        setNotice(undefined);
        return;
      }

      let meetingStatus: MeetingStatus = 'not-in-meeting';
      let contentScriptMissing = false;
      if (isMeetTab(tab)) {
        try {
          const result = await chrome.tabs.sendMessage(tab.id, { type: 'GET_MEETING_STATUS' }) as { status?: MeetingStatus };
          meetingStatus = result?.status ?? 'unknown';
        } catch {
          meetingStatus = 'unknown';
          contentScriptMissing = true;
        }
      }

      const current = await chrome.runtime.sendMessage({ type: 'GET_STATE', tabId: tab.id }) as ExtensionState | undefined;
      if (sequence !== refreshSequence.current) return;
      setState((previous) => {
        if (current && (previous.revision ?? 0) > (current.revision ?? 0)) {
          return { ...previous, meetingStatus };
        }
        return { ...initialState, ...(current ?? {}), meetingStatus };
      });
      const tabChanged = previousTabId !== tab.id;
      if (contentScriptMissing) {
        setNotice({ tone: 'info', text: 'Refresh this Meet tab once to connect the extension controls.' });
      } else if (tabChanged || contentScriptMissingRef.current) {
        setNotice(undefined);
      }
      contentScriptMissingRef.current = contentScriptMissing;
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Could not read the active tab.' });
    }
  }, []);

  const syncGoogleDriveStatus = useCallback(async () => {
    try {
      const connected = await readGoogleDriveStatus();
      setGoogleDriveConnection(connected ? 'connected' : 'disconnected');
      setGoogleDriveError(undefined);
      if (connected) {
        setGoogleDriveAuthorizing(false);
      }
      return connected;
    } catch (error) {
      setGoogleDriveConnection('error');
      setGoogleDriveError(error instanceof Error ? error.message : 'Could not check Google Drive connection.');
      return false;
    }
  }, []);

  useEffect(() => {
    const updateFromBroadcast = (next: ExtensionState) => {
      setState((previous) => ({
        ...(next.revision !== undefined && (previous.revision ?? 0) > next.revision ? previous : {
          ...previous,
          ...next,
          meetingStatus: next.meetingTabId === activeTabId.current ? next.meetingStatus : previous.meetingStatus,
        }),
      }));
      if (next.captureStatus === 'error' && next.error) setNotice(undefined);
      if (next.captureStatus === 'connecting' || next.captureStatus === 'capturing') setNotice(undefined);
    };

    const onMessage = (message: ExtensionMessage) => {
      if (message.type === 'STATE') updateFromBroadcast(message.state);
      if (message.type === 'MIC_PERMISSION_RESULT') {
        microphoneRequestTabId.current = undefined;
        setMicrophone(message.granted ? 'granted' : 'denied');
        setNotice(message.granted
          ? { tone: 'success', text: 'Microphone access is ready. Start capture when you are ready.' }
          : { tone: 'error', text: `Brave did not grant microphone access${message.errorName ? ` (${message.errorName})` : ''}. The permission prompt is managed by Brave.` });
      }
    };
    const onStorageChange = (changes: Record<string, chrome.storage.StorageChange>, areaName: string) => {
      if (areaName === 'session' && changes.state?.newValue) updateFromBroadcast(changes.state.newValue as ExtensionState);
      if (areaName === 'local' && MICROPHONE_PERMISSION_KEY in changes) {
        const granted = changes[MICROPHONE_PERMISSION_KEY]?.newValue === true;
        setMicrophone((current) => granted ? 'granted' : current === 'denied' ? 'denied' : 'prompt');
      }
      if (areaName === 'local' && changes[GOOGLE_DRIVE_AUTH_RESULT_KEY]?.newValue) {
        const result = changes[GOOGLE_DRIVE_AUTH_RESULT_KEY].newValue as { connected?: boolean; error?: string };
        if (result.connected) {
          setGoogleDriveConnection('connected');
          setGoogleDriveAuthorizing(false);
          setGoogleDriveError(undefined);
        } else if (result.error) {
          setGoogleDriveConnection('disconnected');
          setGoogleDriveAuthorizing(false);
          setGoogleDriveError(result.error);
        }
        void chrome.storage.local.remove(GOOGLE_DRIVE_AUTH_RESULT_KEY);
      }
    };
    const onActivated = () => { void refresh(); };
    const onUpdated: Parameters<typeof chrome.tabs.onUpdated.addListener>[0] = (tabId, change, tab) => {
      if (change.status === 'complete' && tab.active && tabId === activeTabId.current) void refresh();
    };
    const onRemoved = (tabId: number) => {
      if (tabId === microphoneRequestTabId.current) {
        microphoneRequestTabId.current = undefined;
        void readMicrophonePermission().then((permission) => {
          if (permission === undefined) return;
          setMicrophone(permission === 'granted' ? 'granted' : permission === 'denied' ? 'denied' : 'prompt');
          void chrome.storage.local.set({ [MICROPHONE_PERMISSION_KEY]: permission === 'granted' });
        });
      }
    };

    chrome.runtime.onMessage.addListener(onMessage);
    chrome.storage.onChanged.addListener(onStorageChange);
    chrome.tabs.onActivated.addListener(onActivated);
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.onRemoved.addListener(onRemoved);
    void refresh();
    const statusTimer = window.setInterval(() => { void refresh(); }, 3_000);

    return () => {
      window.clearInterval(statusTimer);
      chrome.runtime.onMessage.removeListener(onMessage);
      chrome.storage.onChanged.removeListener(onStorageChange);
      chrome.tabs.onActivated.removeListener(onActivated);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      chrome.tabs.onRemoved.removeListener(onRemoved);
      const tabId = activeTabId.current;
      if (tabId !== undefined) {
        chrome.tabs.sendMessage(tabId, { type: 'PANEL_VISIBILITY', visible: false, tabId }).catch(() => undefined);
      }
    };
  }, [refresh, syncGoogleDriveStatus]);

  useEffect(() => {
    let mounted = true;
    void (async () => {
      const connected = await syncGoogleDriveStatus();
      const stored = await chrome.storage.local.get(GOOGLE_DRIVE_AUTH_RESULT_KEY);
      const result = stored[GOOGLE_DRIVE_AUTH_RESULT_KEY] as { connected?: boolean; error?: string; completedAt?: number } | undefined;
      if (!mounted || !result) return;
      const recent = typeof result.completedAt === 'number' && Date.now() - result.completedAt < 2 * 60 * 1000;
      if (!connected && recent && result.error) {
        setGoogleDriveError(result.error);
        setGoogleDriveConnection('disconnected');
      }
      await chrome.storage.local.remove(GOOGLE_DRIVE_AUTH_RESULT_KEY);
    })();
    return () => {
      mounted = false;
    };
  }, [syncGoogleDriveStatus]);

  useEffect(() => {
    if (!googleDriveAuthorizing) return;
    const poll = window.setInterval(() => { void syncGoogleDriveStatus(); }, 1_500);
    return () => window.clearInterval(poll);
  }, [googleDriveAuthorizing, syncGoogleDriveStatus]);

  useEffect(() => {
    let permissionStatus: PermissionStatus | undefined;
    let cancelled = false;
    void (async () => {
      const saved = await chrome.storage.local.get(MICROPHONE_PERMISSION_KEY);
      if (cancelled) return;
      let remembered = saved[MICROPHONE_PERMISSION_KEY] === true;
      try {
        const permission = await navigator.permissions.query({ name: 'microphone' as PermissionName });
        if (cancelled) return;
        permissionStatus = permission;
        const applyPermission = () => {
          if (cancelled) return;
          const granted = permission.state === 'granted';
          const next = granted ? 'granted' : permission.state === 'denied' ? 'denied' : 'prompt';
          setMicrophone((current) => current === 'requesting' && next === 'prompt' ? current : next);
          if (remembered !== granted) {
            remembered = granted;
            void chrome.storage.local.set({ [MICROPHONE_PERMISSION_KEY]: granted });
          }
        };
        applyPermission();
        permission.onchange = applyPermission;
      } catch {
        // Without a browser permission query, do not promote a stale stored
        // flag to granted. The foreground request can verify access again.
        if (!cancelled) setMicrophone((current) => current === 'requesting' ? current : 'prompt');
      }
    })();
    return () => {
      cancelled = true;
      if (permissionStatus) permissionStatus.onchange = null;
    };
  }, []);

  useEffect(() => {
    const tabId = activeTab?.id;
    if (!tabId || !isMeetTab(activeTab) || state.meetingStatus !== 'in-meeting' || microphone !== 'granted') {
      setReadyTabId(undefined);
      return;
    }
    if (state.captureStatus === 'capturing' || state.captureStatus === 'connecting') {
      setReadyTabId(tabId);
      return;
    }
    let cancelled = false;
    setReadyTabId(undefined);
    void chrome.runtime.sendMessage({ type: 'PREPARE_CAPTURE', tabId }).then((result: { ok?: boolean; error?: string } | undefined) => {
      if (cancelled) return;
      if (result?.ok) setReadyTabId(tabId);
      else setNotice({ tone: 'error', text: result?.error ?? 'Could not prepare capture.' });
    }).catch((error: unknown) => {
      if (!cancelled) setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Could not prepare capture.' });
    });
    return () => { cancelled = true; };
  }, [activeTab?.id, state.meetingStatus, microphone]);

  const requestMicrophone = async () => {
    setBusy(true);
    setMicrophone('requesting');
    setNotice({ tone: 'info', text: 'Opening Brave’s native microphone permission prompt…' });
    try {
      const tab = await chrome.tabs.create({ url: chrome.runtime.getURL('mic-permission.html'), active: true });
      if (tab.id === undefined) throw new Error('Brave could not open the microphone permission request.');
      microphoneRequestTabId.current = tab.id;
    } catch (error) {
      setMicrophone('prompt');
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Could not open Brave’s microphone permission prompt.' });
    } finally {
      setBusy(false);
    }
  };

  const connectGoogleDrive = async () => {
    setGoogleDriveBusy(true);
    setGoogleDriveAuthorizing(true);
    setGoogleDriveError(undefined);
    try {
      let response: Response;
      try {
        response = await fetch(GOOGLE_DRIVE_AUTH_START_URL, { cache: 'no-store' });
      } catch {
        throw new Error('Could not reach the local backend. Make sure it is running, then retry.');
      }
      const payload = await response.json().catch(() => ({})) as {
        connected?: boolean;
        authorization_url?: string | null;
        redirect_uri?: string;
        detail?: { message?: string };
      };
      if (!response.ok) {
        throw new Error(payload.detail?.message ?? `Google Drive setup failed (${response.status}).`);
      }
      if (payload.connected || !payload.authorization_url) {
        setGoogleDriveConnection('connected');
        setGoogleDriveAuthorizing(false);
        return;
      }
      if (!payload.redirect_uri) throw new Error('The backend did not provide a Google OAuth callback URL.');

      const authUrl = new URL(payload.authorization_url);
      const callbackUrl = new URL(payload.redirect_uri);
      if (authUrl.origin !== 'https://accounts.google.com') {
        throw new Error('The backend returned an unexpected Google sign-in address.');
      }
      if (!['http:', 'https:'].includes(callbackUrl.protocol)) {
        throw new Error('The Google OAuth callback URL is invalid.');
      }

      const tab = await chrome.tabs.create({ url: authUrl.toString(), active: true });
      if (tab.id === undefined) throw new Error('Could not open the Google sign-in tab.');
      await chrome.storage.local.set({
        [GOOGLE_DRIVE_AUTH_TAB_KEY]: {
          tabId: tab.id,
          redirectUri: callbackUrl.toString(),
        },
      });
    } catch (error) {
      setGoogleDriveAuthorizing(false);
      setGoogleDriveConnection('error');
      setGoogleDriveError(error instanceof Error ? error.message : 'Could not start Google Drive sign-in.');
    } finally {
      setGoogleDriveBusy(false);
    }
  };

  const startCapture = async () => {
    if (!activeTab?.id || state.meetingStatus !== 'in-meeting') {
      setNotice({ tone: 'info', text: 'Join a Google Meet call before starting capture.' });
      return;
    }
    if (microphone !== 'granted') {
      setNotice({ tone: 'info', text: 'Enable microphone access here first.' });
      return;
    }
    if (readyTabId !== activeTab.id) {
      setNotice({ tone: 'info', text: 'Preparing the audio capture. Try again in a moment.' });
      return;
    }
    setBusy(true);
    setNotice({ tone: 'info', text: 'Starting audio-only capture from this Meet tab and your microphone…' });
    // Send immediately so tabCapture runs in the user-initiated message path.
    const request = chrome.runtime.sendMessage({ type: 'START_CAPTURE', tabId: activeTab.id }) as Promise<{ ok?: boolean; error?: string; state?: ExtensionState } | undefined>;
    try {
      const response = await request;
      if (!response?.ok) setNotice({ tone: 'error', text: response?.error ?? response?.state?.error ?? 'Could not start capture.' });
      else setNotice(undefined);
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Could not start capture.' });
    } finally {
      setBusy(false);
    }
  };

  const stopCapture = async () => {
    setBusy(true);
    setNotice(undefined);
    try {
      const response = await chrome.runtime.sendMessage({ type: 'STOP_CAPTURE' }) as { ok?: boolean; error?: string } | undefined;
      if (!response?.ok) setNotice({ tone: 'error', text: response?.error ?? 'Could not stop capture. Please try again.' });
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Could not stop capture.' });
    } finally {
      setBusy(false);
    }
  };

  const closePanel = async () => {
    const sidePanel = chrome.sidePanel as unknown as SidePanelCloseApi;
    if (!sidePanel.close) {
      setNotice({ tone: 'info', text: 'Use the Meeting Intelligence toolbar button to close this panel in this Brave version.' });
      return;
    }
    try {
      if (activeTab?.id !== undefined) {
        try {
          await sidePanel.close({ tabId: activeTab.id });
          return;
        } catch {
          // A global panel may need to be closed by window ID instead.
        }
      }
      if (activeTab?.windowId !== undefined) {
        await sidePanel.close({ windowId: activeTab.windowId });
      } else {
        throw new Error('Could not identify the browser window to close this panel.');
      }
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Could not close the Meeting Intelligence panel.' });
    }
  };

  const running = state.captureStatus === 'capturing' || state.captureStatus === 'connecting';
  const capturingHere = running && state.captureTabId === activeTab?.id;
  const capturingElsewhere = running && !capturingHere;
  const inMeeting = isMeetTab(activeTab) && state.meetingStatus === 'in-meeting';
  const tabAudioAuthorized = activeTab?.id !== undefined && state.tabAudioAuthorizedTabIds?.includes(activeTab.id) === true;
  const statusTitle = capturingHere
    ? state.captureStatus === 'connecting' ? 'Starting your capture' : 'Meeting is being captured'
    : capturingElsewhere ? 'Capture active in another tab'
      : inMeeting ? 'You’re in a Google Meet call'
        : state.meetingStatus === 'unknown' && isMeetTab(activeTab) ? 'Connect this Meet tab'
          : 'Ready when you are';
  const jevDecisionCopy = state.jevDecision?.action === 'SEARCH_EXISTING_WORK'
    ? { title: 'Search existing work', detail: 'The conversation points to work that may already be tracked.' }
    : state.jevDecision?.action === 'SUGGEST_NEW_TASK'
      ? { title: 'Suggest a new task', detail: 'The conversation introduced a clear follow-up worth tracking.' }
      : undefined;

  return (
    <main className="shell">
      <header className="header">
        <div className="brand-mark" aria-hidden="true">M</div>
        <div className="brand-copy"><h1>Meeting Intelligence</h1><p>Real-time meeting audio</p></div>
        <span className={`live-indicator ${running ? 'is-live' : ''}`} aria-label={running ? 'Capture active' : 'Capture inactive'} />
        <button className="panel-close" onClick={() => void closePanel()} aria-label="Close Meeting Intelligence panel" title="Close panel">×</button>
      </header>

      <section className={`meeting-card ${running ? 'is-active' : inMeeting ? 'is-ready' : ''}`} aria-live="polite">
        <div className="status-icon" aria-hidden="true">{running ? '●' : inMeeting ? '✓' : '•'}</div>
        <div className="status-copy">
          <strong>{statusTitle}</strong>
          <span>{running
            ? state.backendConnected ? 'Connected to your local backend' : 'Connecting to localhost:8000…'
            : inMeeting ? tabAudioAuthorized ? 'Tab audio is authorized; no video is requested' : 'Authorize tab audio with the extension toolbar icon' : isMeetTab(activeTab) ? 'Join the call to enable capture' : 'Open or join Google Meet to get started'}</span>
        </div>
      </section>

      {capturingElsewhere && <p className="context-note">Your audio session is running in another tab. Stop it here before starting a new one.</p>}
      {state.captureStatus === 'error' && state.error
        ? <div className="notice error" role="alert">{state.error}</div>
        : notice && <div className={`notice ${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}>{notice.text}</div>}

      {state.jevDecision && jevDecisionCopy && (
        <section className="decision-card" aria-live="polite">
          <div className="decision-label">Jev decision</div>
          <strong>{jevDecisionCopy.title}</strong>
          <p>{jevDecisionCopy.detail}</p>
          <span>{Math.round(state.jevDecision.confidence * 100)}% confidence</span>
        </section>
      )}

      {state.relatedFiles !== undefined && (
        <section className="related-files-card" aria-live="polite">
          <div className="related-files-heading">
            <div>
              <h2>Relevant files</h2>
              {state.relatedFilesQuery && <p>{state.relatedFilesQuery}</p>}
            </div>
            <span aria-hidden="true">↗</span>
          </div>
          {state.relatedFiles.length > 0 && (
            <ul className="related-files-list">
              {state.relatedFiles.map((file) => (
                <li key={file.id}>
                  <div className="related-file-copy">
                    <strong title={file.name}>{file.name}</strong>
                    <span>{driveFileType(file.mime_type)}</span>
                  </div>
                  <a
                    href={googleDriveFileUrl(file)}
                    target="_blank"
                    rel="noreferrer"
                  >Open</a>
                </li>
              ))}
            </ul>
          )}
          {state.relatedFilesError && <p className="related-files-empty error-text">{state.relatedFilesError}</p>}
          {!state.relatedFilesError && state.relatedFiles.length === 0 && <p className="related-files-empty">No relevant files found for this discussion yet.</p>}
        </section>
      )}

      <section className="section-card drive-card" aria-live="polite">
        <div className="drive-card-topline">
          <div className="section-heading">
            <span className={`drive-icon ${googleDriveConnection === 'connected' ? 'enabled' : ''}`} aria-hidden="true">G</span>
            <div>
              <h2>Google Drive</h2>
              <p>{googleDriveConnection === 'connected'
                ? 'Connected for related file search'
                : googleDriveAuthorizing ? 'Finish sign-in in the Google tab'
                  : googleDriveConnection === 'checking' ? 'Checking connection…'
                    : 'Connect once to find files mentioned in meetings'}</p>
            </div>
          </div>
          {googleDriveConnection === 'connected' && <span className="drive-connected-badge">Connected</span>}
        </div>
        {googleDriveError && <p className="drive-error" role="status">{googleDriveError}</p>}
        {googleDriveConnection !== 'connected' && (
          <button
            className="drive-connect-button"
            onClick={() => googleDriveConnection === 'error' ? void syncGoogleDriveStatus() : void connectGoogleDrive()}
            disabled={googleDriveBusy || googleDriveAuthorizing || googleDriveConnection === 'checking'}
          >
            {googleDriveConnection === 'checking' ? 'Checking…'
              : googleDriveBusy ? 'Opening Google sign-in…'
                : googleDriveAuthorizing ? 'Waiting for Google sign-in…'
                  : googleDriveConnection === 'error' ? 'Retry connection check' : 'Connect Google Drive'}
          </button>
        )}
      </section>

      <section className="section-card">
        <div className="section-heading">
          <span className={`mic-icon ${tabAudioAuthorized ? 'enabled' : ''}`} aria-hidden="true">{tabAudioAuthorized ? '✓' : '♪'}</span>
          <div><h2>Meet tab audio</h2><p>{tabAudioAuthorized ? 'Authorized for audio-only capture' : 'Needs authorization on this tab'}</p></div>
        </div>
        {inMeeting && !tabAudioAuthorized && <p className="privacy-note">Click the Meeting Intelligence icon in Brave’s toolbar while this Meet tab is active. The panel will update here automatically.</p>}
        {tabAudioAuthorized && <p className="privacy-note">No screen, window, tab video, or camera stream is requested.</p>}
      </section>

      <section className="section-card">
        <div className="section-heading">
          <span className={`mic-icon ${microphone === 'granted' ? 'enabled' : ''}`} aria-hidden="true">{microphone === 'granted' ? '✓' : '♪'}</span>
          <div><h2>Microphone</h2><p>{microphone === 'granted' ? 'Access is enabled' : microphone === 'denied' ? 'Permission needs attention' : 'Needed to include your voice'}</p></div>
        </div>
        {microphone === 'denied' && (
          <div className="permission-help" role="note">
            <strong>Brave will ask for microphone access</strong>
            <p>Press the button here to open Brave’s standard microphone prompt. If no prompt appears, check <code>brave://extensions</code> → Meeting Intelligence → Details → Site settings → Microphone.</p>
            <p>After you allow access, the request tab closes and this panel updates automatically.</p>
          </div>
        )}
        {microphone !== 'granted' && microphone !== 'checking' && (
          <button className="permission-button" onClick={requestMicrophone} disabled={busy || microphone === 'requesting'}>
            {microphone === 'requesting' ? 'Waiting for permission…' : microphone === 'denied' ? 'Retry microphone access' : 'Enable microphone'}
          </button>
        )}
        <p className="privacy-note">Your microphone and Meet audio are streamed to your local service at <code>localhost:8000</code> only while capture is running.</p>
      </section>

      <div className="actions">
        {running ? (
          <button className="stop-button" onClick={stopCapture} disabled={busy}>
            <span className="stop-square" aria-hidden="true" />{busy ? 'Stopping…' : 'Stop capture'}
          </button>
        ) : (
          <button className="start-button" onClick={startCapture} disabled={busy || !inMeeting || microphone !== 'granted' || !tabAudioAuthorized || readyTabId !== activeTab?.id}>
            {busy ? 'Starting…' : !tabAudioAuthorized && inMeeting ? 'Authorize tab audio first' : readyTabId !== activeTab?.id && inMeeting && microphone === 'granted' ? 'Preparing…' : state.captureStatus === 'error' ? 'Retry capture' : 'Start capture'}
          </button>
        )}
        <p className="action-hint">{running ? 'Capture continues if you close this panel.' : !inMeeting ? 'Capture is only available inside an active Meet call.' : !tabAudioAuthorized ? 'Click the extension toolbar icon on this Meet tab once to authorize tab audio.' : microphone === 'checking' ? 'Checking microphone access…' : microphone !== 'granted' ? 'Enable microphone access to continue.' : 'Only Meet tab audio and your microphone will be captured.'}</p>
      </div>

      <footer className="footer">
        <span>{activeTab?.title ? activeTab.title.length > 42 ? `${activeTab.title.slice(0, 39)}…` : activeTab.title : 'Active browser tab'}</span>
        <button className="refresh-button" onClick={() => void refresh()} aria-label="Refresh status" title="Refresh status">↻</button>
      </footer>
    </main>
  );
}
