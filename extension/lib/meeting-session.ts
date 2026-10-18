import type { MeetingStatus } from './types';

// Meet room codes vary in group length. Ignore the pre-join homepage and
// normalize trailing slashes and URL query parameters to one room identity.
const MEETING_PATH = /^\/([a-z0-9]{2,4}-[a-z0-9]{2,4}-[a-z0-9]{2,4})\/?$/i;
const ZOOM_NUMBER = /^\d{9,11}$/;
export type MeetingPlatform = 'google_meet' | 'zoom';
export const MEETING_MATCHES = ['https://meet.google.com/*', 'https://*.zoom.us/wc/*'];

export function meetingPlatformFromUrl(url: string | undefined): MeetingPlatform | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.port) return undefined;
    if (parsed.hostname === 'meet.google.com') return 'google_meet';
    if ((parsed.hostname === 'zoom.us' || parsed.hostname.endsWith('.zoom.us')) && parsed.pathname.startsWith('/wc/')) return 'zoom';
  } catch { /* Unsupported pages have no meeting identity. */ }
  return undefined;
}

export interface MeetingIdentity {
  tabId: number;
  meetingId: string;
}

export function meetingIdFromUrl(url: string | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    const platform = meetingPlatformFromUrl(url);
    if (platform === 'google_meet') return MEETING_PATH.exec(parsed.pathname)?.[1].toLowerCase();
    if (platform !== 'zoom') return undefined;
    // Regional/vanity hosts and the two Web App routes identify the same room.
    // Never retain the meeting passcode or other query parameters.
    const number = /^\/wc\/(\d{9,11})\/(?:join|start)\/?$/.exec(parsed.pathname)?.[1]
      ?? /^\/wc\/(?:join|start)\/(\d{9,11})\/?$/.exec(parsed.pathname)?.[1]
      ?? (/^\/wc\/(?:join|start)\/?$/.test(parsed.pathname) ? parsed.searchParams.get('confno') : undefined);
    return number && ZOOM_NUMBER.test(number) ? `zoom:${number}` : undefined;
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
