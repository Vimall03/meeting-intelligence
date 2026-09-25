export type MeetingStatus = 'unknown' | 'not-in-meeting' | 'in-meeting';
export type CaptureStatus = 'idle' | 'connecting' | 'capturing' | 'error';

export interface ExtensionState {
  meetingStatus: MeetingStatus;
  meetingTabId?: number;
  captureTabId?: number;
  tabAudioAuthorizedTabIds?: number[];
  revision?: number;
  captureStatus: CaptureStatus;
  backendConnected: boolean;
  error?: string;
}

export type ExtensionMessage =
  | { type: 'MEETING_STATUS'; status: MeetingStatus; tabId: number }
  | { type: 'GET_MEETING_STATUS' }
  | { type: 'GET_STATE'; tabId?: number }
  | { type: 'OPEN_SIDE_PANEL'; tabId: number }
  | { type: 'PANEL_VISIBILITY'; visible: boolean; tabId?: number }
  | { type: 'MIC_PERMISSION_RESULT'; granted: boolean; errorName?: string; errorMessage?: string }
  | { type: 'MICROPHONE_PERMISSION_STATUS'; granted: boolean }
  | { type: 'CLOSE_MIC_PERMISSION_TAB' }
  | { type: 'PREPARE_CAPTURE'; tabId: number }
  | { type: 'START_CAPTURE'; tabId: number }
  | { type: 'STOP_CAPTURE' }
  | { type: 'CAPTURE_STATE'; state: Partial<ExtensionState> }
  | { type: 'STATE'; state: ExtensionState; tabId?: number }
  | { type: 'START_OFFSCREEN'; streamId: string }
  | { type: 'STOP_OFFSCREEN' }
  | { type: 'GET_OFFSCREEN_STATUS' };
