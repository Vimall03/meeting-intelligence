import { detectMeeting } from '../lib/meeting-detection';
import { meetingIdFromUrl } from '../lib/meeting-session';
import type { ExtensionMessage } from '../lib/types';

export default defineContentScript({
  matches: ['https://meet.google.com/*'],
  runAt: 'document_idle',
  main() {
    const report = async () => {
      const status = detectMeeting();
      const meetingId = status === 'in-meeting' ? meetingIdFromUrl(window.location.href) : undefined;
      const meetMicrophoneProblem = Array.from(document.querySelectorAll<HTMLElement>('[aria-label]'))
        .some((element) => /microphone problem/i.test(element.getAttribute('aria-label') ?? ''));
      const message: ExtensionMessage = {
        type: 'MEETING_STATUS', status, meetingId, meetMicrophoneProblem, tabId: -1,
      };
      await chrome.runtime.sendMessage(message).catch(() => undefined);
      return { status, meetingId, meetMicrophoneProblem };
    };

    chrome.runtime.onMessage.addListener((message: ExtensionMessage, _sender, sendResponse) => {
      if (message.type !== 'GET_MEETING_STATUS') return false;
      void report().then(sendResponse);
      return true;
    });

    void report();
    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      window.setTimeout(() => { scheduled = false; void report(); }, 250);
    });
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
    window.setInterval(() => { void report(); }, 3_000);
  },
});
