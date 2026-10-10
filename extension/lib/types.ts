import type { MomSettings, MomMeeting } from './mom';
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
  meetingTabId?: number;
  captureTabId?: number;
  captureSessionId?: string;
  revision?: number;
  captureStatus: CaptureStatus;
  captureMode?: import('./mom').MomMode;
  backendConnected: boolean;
  timeline?: TimelineSnapshot;
  audioWarning?: string;
  timelineOwner?: MeetingIdentity;
  error?: string;
  momSettings?: MomSettings;
  momUserId?: string;
  minutes?: MomMeeting[];
}

export type ExtensionMessage =
  | { type: 'AUTH_STATE' }
  | { type: 'AUTH_REQUEST'; email: string }
  | { type: 'AUTH_VERIFY'; code: string }
  | { type: 'AUTH_LOGOUT' }
  | { type: 'AUTH_FETCH'; url: string; method?: string; body?: string }
  | { type: 'MEETING_TICKET' }
  | { type: 'AUTH_CHANGED' }
  | { type: 'MEETING_STATUS'; status: MeetingStatus; meetingId?: string; tabId: number }
  | { type: 'GET_MEETING_STATUS' }
  | { type: 'CHECK_MEETING_TAB'; tabId: number }
  | { type: 'GET_STATE'; tabId?: number }
  | { type: 'MIC_PERMISSION_RESULT'; granted: boolean; permissionState?: PermissionState; errorName?: string; errorMessage?: string }
  | { type: 'MICROPHONE_PERMISSION_STATUS'; granted: boolean }
  | { type: 'CLOSE_MIC_PERMISSION_TAB' }
  | { type: 'PREPARE_CAPTURE'; tabId: number }
  | { type: 'START_CAPTURE'; tabId: number }
  | { type: 'STOP_CAPTURE'; finishMinutes?: boolean }
  | { type: 'MOM_SETTINGS'; mode?: import('./mom').MomMode; timezone?: string }
  | { type: 'MOM_STATUS'; sessionId: string }
  | { type: 'MOM_FINISH'; sessionId: string }
  | { type: 'MOM_RETRY'; sessionId: string }
  | { type: 'MOM_UPDATE'; userId: string; meeting: MomMeeting }
  | { type: 'CAPTURE_STATE'; captureSessionId?: string; state: Partial<ExtensionState> }
  | { type: 'STATE'; state: ExtensionState; tabId?: number }
  | { type: 'START_OFFSCREEN'; streamId: string; captureSessionId: string; roomId?: string; userId?: string; mode?: import('./mom').MomMode }
  | { type: 'STOP_OFFSCREEN'; finishMinutes?: boolean }
  | { type: 'GET_OFFSCREEN_STATUS' };
