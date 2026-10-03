import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ExtensionMessage, ExtensionState, MeetingStatus, TaskSuggestionItem } from '../../lib/types';
import { isTimelineForMeeting, meetingIdFromUrl } from '../../lib/meeting-session';
import { visibleTimelineItems } from '../../lib/timeline';
import { loadJiraProjects, submitJiraTask } from '../../lib/jira-tasks';
import type { JiraProject } from '../../lib/jira-tasks';
import { MICROPHONE_PERMISSION_KEY, resolveMicrophonePermission, isMicrophonePermissionError } from '../../lib/microphone-permission';
import {
  GOOGLE_DRIVE_AUTH_RESULT_KEY,
  GOOGLE_DRIVE_AUTH_START_URL,
  GOOGLE_DRIVE_AUTH_TAB_KEY,
  GOOGLE_DRIVE_STATUS_URL,
  ATLASSIAN_STATUS_URL,
  ATLASSIAN_AUTH_START_URL,
  ATLASSIAN_JIRA_PROJECTS_URL,
  ATLASSIAN_JIRA_TASKS_URL,
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

async function readAtlassianStatus(): Promise<{ connected: boolean; configured: boolean; site_url?: string; sources: string[]; can_create_tasks: boolean }> {
  const response = await fetch(ATLASSIAN_STATUS_URL, { cache: 'no-store' });
  const payload = await response.json().catch(() => ({})) as {
    connected?: boolean; configured?: boolean; site_url?: string; sources?: string[]; can_create_tasks?: boolean; detail?: { message?: string };
  };
  if (!response.ok) throw new Error(payload.detail?.message ?? `Atlassian status failed (${response.status}).`);
  return { connected: payload.connected === true, configured: payload.configured === true, site_url: payload.site_url,
    sources: payload.sources ?? [], can_create_tasks: payload.can_create_tasks === true };
}

function TaskSuggestionCard({ item, connected, canCreate, connecting, onConnect }: {
  item: TaskSuggestionItem;
  connected: boolean;
  canCreate: boolean;
  connecting: boolean;
  onConnect: (force?: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [projects, setProjects] = useState<JiraProject[]>([]);
  const [projectId, setProjectId] = useState('');
  const [title, setTitle] = useState(item.title);
  const [description, setDescription] = useState(item.description);
  const [error, setError] = useState<string>();
  const [created, setCreated] = useState<{ key: string; url: string }>();

  const openForm = async () => {
    setOpen(true);
    setLoading(true);
    setError(undefined);
    try {
      const available = await loadJiraProjects(ATLASSIAN_JIRA_PROJECTS_URL);
      setProjects(available);
      if (available.length === 1) setProjectId(available[0].id);
      if (available.length === 0) setError('No Jira projects are available on this site.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load Jira projects. Check the backend connection.');
    } finally {
      setLoading(false);
    }
  };

  const createTask = async () => {
    if (savingRef.current || !projectId) return;
    savingRef.current = true;
    setSaving(true);
    setError(undefined);
    try {
      const payload = await submitJiraTask(ATLASSIAN_JIRA_TASKS_URL, {
        project_id: projectId, title: title.trim(), description: description.trim(),
      });
      setCreated({ key: payload.key, url: payload.url });
      setOpen(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not create the task. Check Jira before retrying.');
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  if (created) return <p className="task-created" role="status">Created <a href={created.url} target="_blank" rel="noreferrer">{created.key} ↗</a></p>;
  if (!connected) return <button className="task-action" type="button" disabled={connecting} onClick={() => onConnect()}>{connecting ? 'Connecting Atlassian…' : 'Connect Atlassian to create'}</button>;
  if (!canCreate) return <button className="task-action" type="button" disabled={connecting} onClick={() => onConnect(true)}>{connecting ? 'Waiting for Jira access…' : 'Enable Jira task creation'}</button>;
  return <div className="task-create">
    {!open ? <button className="task-action" type="button" onClick={() => void openForm()}>Create Jira task</button> : <>
      <label className="task-field">Jira project
        <select value={projectId} onChange={(event) => setProjectId(event.target.value)} disabled={loading || saving}>
          <option value="">{loading ? 'Loading projects…' : 'Select a project'}</option>
          {projects.map((project) => <option key={project.id} value={project.id}>{project.name} ({project.key})</option>)}
        </select>
      </label>
      <p className="task-project-help">The task appears on boards that include this project.</p>
      <label className="task-field">Title
        <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={255} disabled={saving} />
      </label>
      <label className="task-field">Description
        <textarea value={description} onChange={(event) => setDescription(event.target.value)} maxLength={5000} rows={3} disabled={saving} />
      </label>
      <div className="task-create-actions">
        <button type="button" onClick={() => { setOpen(false); setError(undefined); }} disabled={saving}>Cancel</button>
        <button type="button" onClick={() => void createTask()} disabled={saving || loading || !projectId || !title.trim() || !description.trim()}>{saving ? 'Creating…' : 'Create task'}</button>
      </div>
    </>}
    {error && <p className="task-error" role="alert">{error}</p>}
  </div>;
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

function searchResultUrl(file: { id: string; web_view_link?: string }, sourceId: string): string | undefined {
  if (sourceId === 'google_drive') return googleDriveFileUrl(file);
  if (!file.web_view_link) return undefined;
  try {
    const url = new URL(file.web_view_link);
    if (url.protocol === 'https:' && url.hostname.endsWith('.atlassian.net')) return url.toString();
  } catch {
    // Ignore malformed provider links.
  }
  return undefined;
}

function driveFileType(mimeType: string) {
  if (mimeType === 'application/vnd.atlassian.jira.issue') return 'Jira issue';
  if (mimeType === 'application/vnd.atlassian.confluence.page') return 'Confluence page';
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

function timelineTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
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
  const extensionVersion = chrome.runtime.getManifest().version;
  const [state, setState] = useState(initialState);
  const [activeTab, setActiveTab] = useState<chrome.tabs.Tab>();
  const [microphone, setMicrophone] = useState<MicrophonePermission>('checking');
  const [googleDriveConnection, setGoogleDriveConnection] = useState<GoogleDriveConnection>('checking');
  const [googleDriveAuthorizing, setGoogleDriveAuthorizing] = useState(false);
  const [googleDriveBusy, setGoogleDriveBusy] = useState(false);
  const [googleDriveError, setGoogleDriveError] = useState<string>();
  const [atlassianConnection, setAtlassianConnection] = useState<GoogleDriveConnection>('checking');
  const [atlassianConfigured, setAtlassianConfigured] = useState(false);
  const [atlassianSite, setAtlassianSite] = useState<string>();
  const [atlassianSources, setAtlassianSources] = useState<string[]>([]);
  const [atlassianCanCreateTasks, setAtlassianCanCreateTasks] = useState(false);
  const [atlassianAuthorizing, setAtlassianAuthorizing] = useState(false);
  const [atlassianBusy, setAtlassianBusy] = useState(false);
  const [atlassianError, setAtlassianError] = useState<string>();
  const [notice, setNotice] = useState<Notice>();
  const [busy, setBusy] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [readyTabId, setReadyTabId] = useState<number>();
  const activeTabId = useRef<number | undefined>(undefined);
  const microphoneRequestTabId = useRef<number | undefined>(undefined);
  const refreshSequence = useRef(0);
  const contentScriptMissingRef = useRef(false);
  const atlassianNeedsWriteRef = useRef(false);
  const timelineScrollRef = useRef<HTMLDivElement>(null);
  const previousTimelineLayout = useRef<{ firstId?: string; height: number }>({ height: 0 });

  useLayoutEffect(() => {
    const scroll = timelineScrollRef.current;
    if (!scroll) return;
    const firstId = state.timeline?.items[0]?.id;
    const previous = previousTimelineLayout.current;
    if (previous.firstId && firstId !== previous.firstId && scroll.scrollTop > 24) {
      scroll.scrollTop += scroll.scrollHeight - previous.height;
    }
    previousTimelineLayout.current = { firstId, height: scroll.scrollHeight };
  }, [state.timeline?.session_id, state.timeline?.revision]);

  const refresh = useCallback(async () => {
    const sequence = ++refreshSequence.current;
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (sequence !== refreshSequence.current) return;
      if (tab?.url === chrome.runtime.getURL('mic-permission.html')) return;
      const previousTabId = activeTabId.current;
      setActiveTab(tab);
      activeTabId.current = tab?.id ?? undefined;
      if (!tab?.id) {
        setState(initialState);
        setNotice(undefined);
        return;
      }

      let meetingStatus: MeetingStatus = 'not-in-meeting';
      let contentScriptMissing = false;
      if (isMeetTab(tab)) {
        try {
          const result = await chrome.runtime.sendMessage({ type: 'CHECK_MEETING_TAB', tabId: tab.id }) as {
            status?: MeetingStatus; contentScriptMissing?: boolean;
          };
          meetingStatus = result?.status ?? 'unknown';
          contentScriptMissing = result?.contentScriptMissing === true;
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
        setNotice({ tone: 'error', text: 'Could not reconnect the extension to this Meet tab. Refresh the Meet tab once, then retry.' });
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

  const syncAtlassianStatus = useCallback(async () => {
    try {
      const status = await readAtlassianStatus();
      setAtlassianConnection(status.connected ? 'connected' : 'disconnected');
      setAtlassianConfigured(status.configured);
      setAtlassianSite(status.site_url);
      setAtlassianSources(status.sources);
      setAtlassianCanCreateTasks(status.can_create_tasks);
      if (status.connected && (!atlassianNeedsWriteRef.current || status.can_create_tasks)) {
        setAtlassianAuthorizing(false);
        atlassianNeedsWriteRef.current = false;
        setAtlassianError(undefined);
      }
      return status.connected;
    } catch (error) {
      setAtlassianConnection('error');
      setAtlassianError(error instanceof Error ? error.message : 'Could not check Atlassian connection.');
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
        if (message.granted || isMicrophonePermissionError(message.errorName)) {
          setMicrophone(message.granted ? 'granted' : 'denied');
        } else {
          void chrome.storage.local.get(MICROPHONE_PERMISSION_KEY).then((saved) => setMicrophone(resolveMicrophonePermission(undefined, saved[MICROPHONE_PERMISSION_KEY] === true)));
        }
        setNotice(message.granted
          ? { tone: 'success', text: 'Microphone access is ready. Start capture when you are ready.' }
          : { tone: 'error', text: message.errorMessage || `Could not open the microphone${message.errorName ? ` (${message.errorName})` : ''}. Check your input device and Brave microphone settings.` });
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
        void readMicrophonePermission().then(async (permission) => {
          const saved = await chrome.storage.local.get(MICROPHONE_PERMISSION_KEY);
          setMicrophone(resolveMicrophonePermission(permission, saved[MICROPHONE_PERMISSION_KEY] === true));
          if (permission === 'denied') await chrome.storage.local.set({ [MICROPHONE_PERMISSION_KEY]: false });
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

  useEffect(() => { void syncAtlassianStatus(); }, [syncAtlassianStatus]);
  useEffect(() => {
    if (!atlassianAuthorizing) return;
    const poll = window.setInterval(() => { void syncAtlassianStatus(); }, 1_500);
    const timeout = window.setTimeout(() => {
      setAtlassianAuthorizing(false);
      setAtlassianError('Atlassian sign-in is still pending. Finish it in the opened tab, then retry.');
    }, 5 * 60_000);
    return () => { window.clearInterval(poll); window.clearTimeout(timeout); };
  }, [atlassianAuthorizing, syncAtlassianStatus]);

  useEffect(() => {
    let permissionStatus: PermissionStatus | undefined;
    let cancelled = false;
    void (async () => {
      const saved = await chrome.storage.local.get(MICROPHONE_PERMISSION_KEY);
      if (cancelled) return;
      const remembered = saved[MICROPHONE_PERMISSION_KEY] === true;
      try {
        const permission = await navigator.permissions.query({ name: 'microphone' as PermissionName });
        if (cancelled) return;
        permissionStatus = permission;
        const applyPermission = async () => {
          const latest = await chrome.storage.local.get(MICROPHONE_PERMISSION_KEY);
          if (cancelled) return;
          const next = resolveMicrophonePermission(permission.state, latest[MICROPHONE_PERMISSION_KEY] === true);
          setMicrophone((current) => current === 'requesting' && next === 'prompt' ? current : next);
          if (permission.state !== 'prompt') {
            await chrome.storage.local.set({ [MICROPHONE_PERMISSION_KEY]: permission.state === 'granted' });
          }
        };
        applyPermission();
        permission.onchange = applyPermission;
      } catch {
        // Capture revalidates a remembered grant with getUserMedia.
        if (!cancelled) setMicrophone((current) => current === 'requesting' ? current : resolveMicrophonePermission(undefined, remembered));
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

  const connectAtlassian = async (force = false) => {
    setAtlassianBusy(true);
    atlassianNeedsWriteRef.current = force;
    setAtlassianError(undefined);
    try {
      const response = await fetch(`${ATLASSIAN_AUTH_START_URL}${force ? '?force=true' : ''}`, { cache: 'no-store' });
      const payload = await response.json().catch(() => ({})) as {
        connected?: boolean; authorization_url?: string | null; detail?: { message?: string };
      };
      if (!response.ok) throw new Error(payload.detail?.message ?? `Atlassian setup failed (${response.status}).`);
      if (payload.connected && !force) { await syncAtlassianStatus(); return; }
      if (!payload.authorization_url) throw new Error('Atlassian did not provide a sign-in URL.');
      const url = new URL(payload.authorization_url);
      if (url.origin !== 'https://auth.atlassian.com') throw new Error('Unexpected Atlassian sign-in address.');
      setAtlassianAuthorizing(true);
      await chrome.tabs.create({ url: url.toString(), active: true });
    } catch (error) {
      setAtlassianAuthorizing(false);
      atlassianNeedsWriteRef.current = false;
      setAtlassianError(error instanceof Error ? error.message : 'Could not start Atlassian sign-in.');
    } finally {
      setAtlassianBusy(false);
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
    setSettingsOpen(false);
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
  const statusTitle = capturingHere
    ? state.captureStatus === 'connecting' ? 'Starting your capture' : 'Meeting is being captured'
    : capturingElsewhere ? 'Capture active in another tab'
      : inMeeting ? 'You’re in a Google Meet call'
        : state.meetingStatus === 'unknown' && isMeetTab(activeTab) ? 'Connect this Meet tab'
          : 'Ready when you are';
  const timelineItems = isTimelineForMeeting(
    state.timelineOwner,
    activeTab?.id,
    meetingIdFromUrl(activeTab?.url),
    state.meetingStatus,
  ) ? visibleTimelineItems(state.timeline) : [];

  useEffect(() => {
    if (running) setSettingsOpen(false);
  }, [running]);

  return (
    <main className="shell">
      <header className="header">
        <div className="brand-mark" aria-hidden="true">M</div>
        <div className="brand-copy"><h1>Meeting Intelligence</h1><p>Real-time meeting audio · v{extensionVersion}</p></div>
        <span className={`live-indicator ${running ? 'is-live' : ''}`} aria-label={running ? 'Capture active' : 'Capture inactive'} />
        {running && (
          <button
            className="settings-button"
            type="button"
            aria-label={settingsOpen ? 'Hide settings' : 'Show settings'}
            aria-controls="capture-settings"
            aria-expanded={settingsOpen}
            title={settingsOpen ? 'Hide settings' : 'Settings'}
            onClick={() => setSettingsOpen((open) => !open)}
          >⚙</button>
        )}
        <button className="panel-close" onClick={() => void closePanel()} aria-label="Close Meeting Intelligence panel" title="Close panel">×</button>
      </header>

      <section className={`meeting-card ${running ? 'is-active' : inMeeting ? 'is-ready' : ''}`} aria-live="polite">
        <div className="status-icon" aria-hidden="true">{running ? '●' : inMeeting ? '✓' : '•'}</div>
        <div className="status-copy">
          <strong>{statusTitle}</strong>
          <span>{running
            ? state.backendConnected ? 'Connected to your local backend' : 'Connecting to localhost:8000…'
            : inMeeting ? 'Ready for audio-only capture; no video is requested' : isMeetTab(activeTab) ? 'Join the call to enable capture' : 'Open or join Google Meet to get started'}</span>
        </div>
      </section>

      {capturingElsewhere && <p className="context-note">Your audio session is running in another tab. Stop it here before starting a new one.</p>}
      {state.captureStatus === 'error' && state.error
        ? <div className="notice error" role="alert">{state.error}</div>
        : notice && <div className={`notice ${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}>{notice.text}</div>}
      {state.meetMicrophoneProblem && (
        <div className="notice error" role="alert">
          Google Meet cannot access its microphone. Click Meet’s microphone warning and allow the selected input device. Meeting Intelligence will keep Meet audio and pending searches running.
        </div>
      )}

      {running && (
        <div className="actions">
          <button className="stop-button" onClick={stopCapture} disabled={busy}>
            <span className="stop-square" aria-hidden="true" />{busy ? 'Stopping…' : 'Stop capture'}
          </button>
        </div>
      )}

      {capturingHere && <section className="section-card live-transcript" aria-label="Live transcript">
        <strong>Live transcript</strong>
        {state.audioWarning && <p className="task-error" role="status">{state.audioWarning}</p>}
        {state.transcriptionWarning && <p className="task-error" role="status">{state.transcriptionWarning}</p>}
        <p className="transcript-text" aria-live="polite">{state.transcript || 'Listening for speech…'}</p>
      </section>}

      <section className="timeline-section" aria-labelledby="timeline-heading">
        <div className="timeline-heading">
          <div>
            <h2 id="timeline-heading">Results timeline</h2>
            <p>Newest results first</p>
          </div>
          <span className="timeline-count">{timelineItems.length}</span>
        </div>
        <div className="timeline-scroll" ref={timelineScrollRef}>
          {timelineItems.length === 0 ? (
            <p className="timeline-empty">Search results and task suggestions will appear here during a meeting.</p>
          ) : (
            <ol className="timeline-list">
              {timelineItems.map((item) => (
                <li className={`timeline-entry ${item.kind === 'task_suggestion' ? 'is-task' : 'is-search'}`} key={item.id}>
                  <article className="timeline-card">
                    <div className="timeline-meta">
                      <span>{item.kind === 'task_suggestion' ? 'Task suggestion' : 'Search results'}</span>
                      <time dateTime={item.created_at}>{timelineTime(item.created_at)}</time>
                    </div>
                    {item.kind === 'task_suggestion' ? (
                      <>
                        <h3>{item.title}</h3>
                        <p className="timeline-description">{item.description}</p>
                        <span className="timeline-confidence">{Math.round(item.confidence * 100)}% confidence</span>
                        <TaskSuggestionCard item={item} connected={atlassianConnection === 'connected'} canCreate={atlassianCanCreateTasks} connecting={atlassianBusy || atlassianAuthorizing || atlassianConnection === 'checking'} onConnect={(force) => { void connectAtlassian(force); }} />
                      </>
                    ) : (
                      <>
                        <div className="timeline-source"><span className="timeline-source-icon" aria-hidden="true">{item.source.label.charAt(0)}</span>{item.source.label}</div>
                        <h3>{item.query || 'Related files'}</h3>
                        {item.files.length > 0 && (
                          <ul className="timeline-files">
                            {item.files.map((file) => (
                              <li key={file.id}>
                                <div className="timeline-file-copy">
                                  <strong title={file.name}>{file.name}</strong>
                                  <span>{file.subtitle ? `${file.subtitle} · ` : ''}{driveFileType(file.mime_type)}</span>
                                </div>
                                {searchResultUrl(file, item.source.id) && <a href={searchResultUrl(file, item.source.id)} target="_blank" rel="noreferrer">Open</a>}
                              </li>
                            ))}
                          </ul>
                        )}
                      </>
                    )}
                  </article>
                </li>
              ))}
            </ol>
          )}
        </div>
      </section>

      {(!running || settingsOpen) && <div id="capture-settings" className="settings-panel">
      {running && <h2 className="settings-heading">Settings</h2>}
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

      <section className="section-card drive-card" aria-live="polite">
        <div className="drive-card-topline">
          <div className="section-heading">
            <span className={`drive-icon ${atlassianConnection === 'connected' ? 'enabled' : ''}`} aria-hidden="true">A</span>
            <div>
              <h2>Atlassian</h2>
              <p>{atlassianConnection === 'connected'
                ? `Searching ${atlassianSources.includes('confluence') ? 'Jira and Confluence' : 'Jira'}${atlassianSite ? ` · ${new URL(atlassianSite).hostname}` : ''}`
                : atlassianAuthorizing ? 'Finish sign-in in the Atlassian tab'
                  : atlassianConnection === 'checking' ? 'Checking connection…'
                    : atlassianConfigured ? 'Connect to find issues and pages mentioned in meetings'
                      : 'Add Atlassian OAuth credentials to the local backend'}</p>
            </div>
          </div>
          {atlassianConnection === 'connected' && <span className="drive-connected-badge">Connected</span>}
        </div>
        {atlassianError && <p className="drive-error" role="status">{atlassianError}</p>}
        {atlassianConnection !== 'connected' && (
          <button className="drive-connect-button" onClick={() => void connectAtlassian()}
            disabled={atlassianBusy || atlassianAuthorizing || atlassianConnection === 'checking'}>
            {atlassianBusy ? 'Opening Atlassian sign-in…' : atlassianAuthorizing ? 'Waiting for Atlassian sign-in…' : 'Connect Atlassian'}
          </button>
        )}
      </section>

      <section className="section-card">
        <div className="section-heading">
          <span className="mic-icon" aria-hidden="true">♪</span>
          <div><h2>Meet tab audio</h2><p>Access is checked when capture starts</p></div>
        </div>
        <p className="privacy-note">No screen, window, tab video, or camera stream is requested.</p>
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
      </div>}

      {!running && <div className="actions">
          <button className="start-button" onClick={startCapture} disabled={busy || !inMeeting || microphone !== 'granted' || readyTabId !== activeTab?.id}>
            {busy ? 'Starting…' : readyTabId !== activeTab?.id && inMeeting && microphone === 'granted' ? 'Preparing…' : state.captureStatus === 'error' ? 'Retry capture' : 'Start capture'}
          </button>
        <p className="action-hint">{!inMeeting ? 'Capture is only available inside an active Meet call.' : microphone === 'checking' ? 'Checking microphone access…' : microphone !== 'granted' ? 'Enable microphone access to continue.' : 'Only Meet tab audio and your microphone will be captured.'}</p>
      </div>}

      <footer className="footer">
        <span>{activeTab?.title ? activeTab.title.length > 42 ? `${activeTab.title.slice(0, 39)}…` : activeTab.title : 'Active browser tab'}</span>
        <button className="refresh-button" onClick={() => void refresh()} aria-label="Refresh status" title="Refresh status">↻</button>
      </footer>
    </main>
  );
}
