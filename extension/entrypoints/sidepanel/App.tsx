import { useCallback, useEffect, useRef, useState } from 'react';
import type { ExtensionMessage, ExtensionState, MeetingStatus } from '../../lib/types';
import { MICROPHONE_PERMISSION_KEY } from '../../lib/microphone-permission';
import './style.css';

type MicrophonePermission = 'checking' | 'prompt' | 'granted' | 'denied' | 'requesting';
type Notice = { tone: 'error' | 'success' | 'info'; text: string };

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
    };
    const onActivated = () => { void refresh(); };
    const onUpdated: Parameters<typeof chrome.tabs.onUpdated.addListener>[0] = (tabId, change, tab) => {
      if (change.status === 'complete' && tab.active && tabId === activeTabId.current) void refresh();
    };
    const onRemoved = (tabId: number) => {
      if (tabId !== microphoneRequestTabId.current) return;
      microphoneRequestTabId.current = undefined;
      void readMicrophonePermission().then((permission) => {
        if (permission === undefined) return;
        setMicrophone(permission === 'granted' ? 'granted' : permission === 'denied' ? 'denied' : 'prompt');
        void chrome.storage.local.set({ [MICROPHONE_PERMISSION_KEY]: permission === 'granted' });
      });
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
  }, [refresh]);

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
