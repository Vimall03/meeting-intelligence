import type { MeetingIdentity } from './meeting-session';

export type MeetingStatus = 'unknown' | 'not-in-meeting' | 'in-meeting';
export type CaptureStatus = 'idle' | 'connecting' | 'capturing' | 'error';
export interface RelatedDriveFile {
  id: string;
  name: string;
  mime_type: string;
  web_view_link?: string;
  modified_time?: string;
  matched_terms: string[];
  relevance_score: number;
  source?: string;
  subtitle?: string;
}

interface TimelineItemBase {
  id: string;
  sequence: number;
  created_at: string;
}

export interface TaskSuggestionItem extends TimelineItemBase {
  kind: 'task_suggestion';
  title: string;
  description: string;
  confidence: number;
}

export interface SearchResultsItem extends TimelineItemBase {
  kind: 'search_results';
  source: { id: string; label: string };
  query: string;
  files: RelatedDriveFile[];
  error: string | null;
}

export type TimelineItem = TaskSuggestionItem | SearchResultsItem;

export interface TimelineSnapshot {
  type: 'timeline_snapshot';
  session_id: string;
  revision: number;
  items: TimelineItem[];
}

export interface ExtensionState {
  meetingStatus: MeetingStatus;
  meetMicrophoneProblem?: boolean;
  meetingTabId?: number;
  captureTabId?: number;
  captureSessionId?: string;
  revision?: number;
  captureStatus: CaptureStatus;
  backendConnected: boolean;
  timeline?: TimelineSnapshot;
  transcript?: string;
  audioWarning?: string;
  transcriptionWarning?: string;
  timelineOwner?: MeetingIdentity;
  error?: string;
}

export type ExtensionMessage =
  | { type: 'MEETING_STATUS'; status: MeetingStatus; meetingId?: string; meetMicrophoneProblem?: boolean; tabId: number }
  | { type: 'GET_MEETING_STATUS' }
  | { type: 'GET_STATE'; tabId?: number }
  | { type: 'MIC_PERMISSION_RESULT'; granted: boolean; errorName?: string; errorMessage?: string }
  | { type: 'MICROPHONE_PERMISSION_STATUS'; granted: boolean }
  | { type: 'CLOSE_MIC_PERMISSION_TAB' }
  | { type: 'PREPARE_CAPTURE'; tabId: number }
  | { type: 'START_CAPTURE'; tabId: number }
  | { type: 'STOP_CAPTURE' }
  | { type: 'CAPTURE_STATE'; captureSessionId?: string; state: Partial<ExtensionState> }
  | { type: 'STATE'; state: ExtensionState; tabId?: number }
  | { type: 'START_OFFSCREEN'; streamId: string; captureSessionId: string }
  | { type: 'STOP_OFFSCREEN' }
  | { type: 'GET_OFFSCREEN_STATUS' };
