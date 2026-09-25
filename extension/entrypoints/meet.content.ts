import { detectMeeting } from '../lib/meeting-detection';
import type { ExtensionMessage, ExtensionState, MeetingStatus } from '../lib/types';

function mountMeetingControl() {
  const host = document.createElement('div');
  host.id = 'meeting-intelligence-control';
  const root = host.attachShadow({ mode: 'closed' });
  document.documentElement.append(host);

  let meetingStatus: MeetingStatus = 'unknown';
  let thisTabId: number | undefined;
  let extensionState: ExtensionState = { meetingStatus: 'unknown', captureStatus: 'idle', backendConnected: false };
  let panelError: string | undefined;
  let panelOpen = false;
  let dismissed = false;
  root.innerHTML = `
    <style>
      :host { all: initial; position: fixed; z-index: 2147483646; top: 16px; right: 16px; width: min(292px, calc(100vw - 32px)); color: #182230; font: 13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
      * { box-sizing: border-box; }
      .card { padding: 14px; border: 1px solid #dfe5ed; border-radius: 14px; background: rgba(255,255,255,.97); box-shadow: 0 8px 30px rgba(20,35,58,.18); backdrop-filter: blur(12px); }
      .head { display:flex; align-items:center; gap:9px; margin-bottom:10px; }
      .mark { display:grid; width:28px; height:28px; place-items:center; border-radius:8px; color:white; background:#2468d8; font-size:13px; font-weight:750; }
      .title { flex:1; font-size:13px; font-weight:700; }
      .dot { width:8px; height:8px; border-radius:50%; background:#aab4c2; }
      .dot.ready { background:#2c73df; }
      .dot.active { background:#12a66a; }
      .close { display:grid; flex:0 0 28px; width:28px; min-height:28px; place-items:center; padding:0; border:1px solid #e2e7ef; border-radius:8px; color:#64748b; background:transparent; font-size:18px; line-height:1; }
      .close:hover { color:#24364b; background:#f3f6fa; }
      .message { margin:0 0 12px; color:#66758a; font-size:12px; }
      .backend { margin:-5px 0 11px; color:#718096; font-size:11px; }
      .error { margin:0 0 10px; padding:8px 9px; border-radius:8px; color:#a12d2a; background:#fff2f1; font-size:11px; overflow-wrap:anywhere; }
      button { width:100%; min-height:36px; border:0; border-radius:8px; color:#fff; background:#2468d8; font:650 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; cursor:pointer; }
      button:hover { background:#1758bf; }
      button:disabled { opacity:.7; cursor:wait; }
      @media (max-width: 420px) { :host { top:10px; right:10px; width:min(270px,calc(100vw - 20px)); } .card { padding:12px; } }
    </style>
    <section class="card" aria-live="polite">
      <div class="head"><span class="mark" aria-hidden="true">M</span><span class="title">Meeting Intelligence</span><span class="dot"></span><button class="close" type="button" aria-label="Dismiss meeting card" title="Dismiss">×</button></div>
      <p class="message"></p>
      <p class="backend" hidden></p>
      <p class="error" hidden></p>
      <button class="open-button" type="button">Open meeting controls</button>
    </section>`;

  const message = root.querySelector<HTMLElement>('.message')!;
  const dot = root.querySelector<HTMLElement>('.dot')!;
  const backend = root.querySelector<HTMLElement>('.backend')!;
  const error = root.querySelector<HTMLElement>('.error')!;
  const openButton = root.querySelector<HTMLButtonElement>('.open-button')!;
  const dismissButton = root.querySelector<HTMLButtonElement>('.close')!;

  const render = () => {
    const visible = meetingStatus === 'in-meeting' && !panelOpen && !dismissed;
    const active = extensionState.captureStatus === 'capturing' || extensionState.captureStatus === 'connecting';
    host.style.display = visible ? 'block' : 'none';
    dot.className = `dot ${active ? 'active' : visible ? 'ready' : ''}`;
    message.textContent = active
      ? extensionState.captureStatus === 'connecting' ? 'Starting Meet + microphone audio…' : 'Meet + microphone capture is running'
      : 'Google Meet call detected. Open the panel to manage microphone access and capture.';
    backend.hidden = !active;
    backend.textContent = `Local backend: ${extensionState.backendConnected ? 'connected' : 'connecting'}`;
    error.hidden = !panelError && !extensionState.error;
    error.textContent = panelError ?? extensionState.error ?? '';
  };

  dismissButton.addEventListener('click', () => {
    dismissed = true;
    render();
  });

  openButton.addEventListener('click', async () => {
    openButton.disabled = true;
    openButton.textContent = 'Opening controls…';
    try {
      const response = await chrome.runtime.sendMessage({ type: 'OPEN_SIDE_PANEL', tabId: -1 }) as { ok?: boolean; error?: string };
      if (!response?.ok) throw new Error(response?.error ?? 'Could not open the meeting controls.');
      panelError = undefined;
      panelOpen = true;
      render();
      openButton.disabled = false;
      openButton.textContent = 'Open meeting controls';
    } catch (reason) {
      panelError = reason instanceof Error ? reason.message : 'Could not open the meeting controls.';
      error.hidden = false;
      error.textContent = panelError;
      openButton.disabled = false;
      openButton.textContent = 'Try opening controls again';
    }
  });

  chrome.runtime.onMessage.addListener((message: ExtensionMessage, _sender, sendResponse) => {
    if (message.type === 'STATE') {
      if (message.tabId === thisTabId) {
        extensionState = message.state;
        render();
      }
    } else if (message.type === 'PANEL_VISIBILITY' && (message.tabId === undefined || thisTabId === undefined || message.tabId === thisTabId)) {
      if (thisTabId === undefined && message.tabId !== undefined) thisTabId = message.tabId;
      panelOpen = message.visible;
      render();
    } else if (message.type === 'GET_MEETING_STATUS') {
      meetingStatus = detectMeeting();
      render();
      const statusMessage: ExtensionMessage = { type: 'MEETING_STATUS', status: meetingStatus, tabId: -1 };
      chrome.runtime.sendMessage(statusMessage).catch(() => undefined);
      sendResponse({ status: meetingStatus });
      return false;
    }
    return false;
  });

  chrome.runtime.sendMessage({ type: 'GET_STATE' })
    .then((response: ExtensionState | undefined) => {
      if (response) {
        thisTabId = response.meetingTabId;
        extensionState = response;
        render();
      }
    })
    .catch(() => undefined);

  return {
    setMeetingStatus(status: MeetingStatus) {
      meetingStatus = status;
      if (status === 'not-in-meeting') dismissed = false;
      render();
    },
  };
}

export default defineContentScript({
  matches: ['https://meet.google.com/*'],
  runAt: 'document_idle',
  main() {
    const control = mountMeetingControl();
    const report = () => {
      const status = detectMeeting();
      control.setMeetingStatus(status);
      const message: ExtensionMessage = { type: 'MEETING_STATUS', status, tabId: -1 };
      chrome.runtime.sendMessage(message).catch(() => undefined);
    };

    report();
    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (!scheduled) {
        scheduled = true;
        window.setTimeout(() => { scheduled = false; report(); }, 250);
      }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
    window.setInterval(report, 3_000);
  },
});
