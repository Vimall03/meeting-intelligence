import { detectMeeting } from '../lib/meeting-detection';
import { meetingIdFromUrl } from '../lib/meeting-session';
import type { ExtensionMessage } from '../lib/types';

export default defineContentScript({
  matches: ['https://meet.google.com/*'],
  runAt: 'document_idle',
  main(ctx) {
    const report = async () => {
      const status = detectMeeting();
      const meetingId = status === 'in-meeting' ? meetingIdFromUrl(window.location.href) : undefined;
      // Meet has its own microphone permission and device controls. Its UI is
      // not evidence that this extension's independently captured mic failed.
      // Actual extension audio health is reported by the offscreen capture.
      const message: ExtensionMessage = {
        type: 'MEETING_STATUS', status, meetingId, tabId: -1,
      };
      if (ctx.isValid) {
        try {
          await chrome.runtime.sendMessage(message);
        } catch {
          // Reloading an unpacked extension invalidates the old page context.
          // The background will inject the current script on its next probe.
        }
      }
      return { status, meetingId };
    };

    const onMessage = (message: ExtensionMessage, _sender: chrome.runtime.MessageSender, sendResponse: (response?: unknown) => void) => {
      if (message.type !== 'GET_MEETING_STATUS') return false;
      void report().then(sendResponse);
      return true;
    };
    chrome.runtime.onMessage.addListener(onMessage);
    ctx.onInvalidated(() => {
      try {
        chrome.runtime.onMessage.removeListener(onMessage);
      } catch {
        // The runtime API itself is unavailable after an unpacked reload.
      }
    });

    void report();
    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      ctx.setTimeout(() => { scheduled = false; void report(); }, 250);
    });
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
    ctx.onInvalidated(() => observer.disconnect());
    ctx.setInterval(() => { void report(); }, 3_000);
  },
});
