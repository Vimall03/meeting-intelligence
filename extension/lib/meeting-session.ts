import type { MeetingStatus } from './types';

// Meet room codes vary in group length. Ignore the pre-join homepage and
// normalize trailing slashes and URL query parameters to one room identity.
const MEETING_PATH = /^\/([a-z0-9]{2,4}-[a-z0-9]{2,4}-[a-z0-9]{2,4})\/?$/i;

export interface MeetingIdentity {
  tabId: number;
  meetingId: string;
}

export function meetingIdFromUrl(url: string | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    if (parsed.hostname !== 'meet.google.com') return undefined;
    return MEETING_PATH.exec(parsed.pathname)?.[1].toLowerCase();
  } catch {
    return undefined;
  }
}

export function isTimelineForMeeting(
  owner: MeetingIdentity | undefined,
  tabId: number | undefined,
  meetingId: string | undefined,
  status: MeetingStatus,
): boolean {
  return status === 'in-meeting'
    && owner !== undefined
    && owner.tabId === tabId
    && owner.meetingId === meetingId;
}

export function didMeetingEnd(
  owner: MeetingIdentity | undefined,
  tabId: number,
  status: MeetingStatus,
  meetingId: string | undefined,
): boolean {
  return owner !== undefined
    && owner.tabId === tabId
    && (status === 'not-in-meeting'
      || (status === 'in-meeting' && meetingId !== undefined && owner.meetingId !== meetingId));
}
