import { problem } from './user-messages';

type SidePanelCloseApi = {
  close?: (options: { tabId?: number; windowId?: number }) => Promise<void>;
};

/** Close only the panel; capture is owned by the offscreen document. */
export async function closeSidePanel(): Promise<void> {
  const sidePanel = chrome.sidePanel as unknown as SidePanelCloseApi;
  if (!sidePanel?.close) throw new Error(problem('MI-APP-003'));
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id !== undefined) {
    try {
      await sidePanel.close({ tabId: tab.id });
      return;
    } catch {
      // A global panel may need to be closed by window ID instead.
    }
  }
  const windowId = tab?.windowId ?? (await chrome.windows.getCurrent()).id;
  if (windowId === undefined) throw new Error(problem('MI-APP-003'));
  await sidePanel.close({ windowId });
}
