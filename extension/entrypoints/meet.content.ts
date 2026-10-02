import { detectMeeting } from '../lib/meeting-detection';
import { meetingIdFromUrl } from '../lib/meeting-session';
import type { ExtensionMessage } from '../lib/types';

export default defineContentScript({
  matches: ['https://meet.google.com/*'],
  runAt: 'document_idle',
  main() {
    const report = () => {
      const status = detectMeeting();
      const meetingId = status === 'in-meeting' ? meetingIdFromUrl(window.location.href) : undefined;
      const message: ExtensionMessage = { type: 'MEETING_STATUS', status, meetingId, tabId: -1 };
      chrome.runtime.sendMessage(message).catch(() => undefined);
      return { status, meetingId };
    };

    chrome.runtime.onMessage.addListener((message: ExtensionMessage, _sender, sendResponse) => {
      if (message.type !== 'GET_MEETING_STATUS') return false;
      sendResponse(report());
      return false;
    });

    report();
    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      window.setTimeout(() => { scheduled = false; report(); }, 250);
    });
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
    window.setInterval(report, 3_000);
  },
});
