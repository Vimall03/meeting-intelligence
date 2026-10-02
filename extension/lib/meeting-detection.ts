import type { MeetingStatus } from './types';
import { meetingIdFromUrl } from './meeting-session';

export function detectMeeting(): MeetingStatus {
  if (!meetingIdFromUrl(window.location.href)) {
    return 'not-in-meeting';
  }

  const labels = Array.from(document.querySelectorAll<HTMLElement>(
    'button, [role="button"], [aria-label], [data-tooltip], [title]',
  )).flatMap((element) => [
    element.getAttribute('aria-label'),
    element.getAttribute('data-tooltip'),
    element.getAttribute('title'),
    element.innerText,
  ]).map((label) => label?.replace(/\s+/g, ' ').trim().toLowerCase() ?? '').filter(Boolean);

  const isPreJoin = labels.some((label) =>
    label.includes('join now') ||
    label.includes('ask to join') ||
    label.includes('request to join'),
  );
  if (isPreJoin) return 'not-in-meeting';

  // A meeting URL alone also covers the pre-join screen. These controls are
  // present once the user has entered the call, and are less brittle than a
  // single Meet-specific class name.
  const hasCallControl = labels.some((label) =>
    label.includes('leave call') ||
    label.includes('hang up') ||
    label.includes('end call') ||
    label.includes('turn off microphone') ||
    label.includes('turn on microphone') ||
    label.includes('turn off camera') ||
    label.includes('turn on camera') ||
    /\b(microphone|mic|camera|video)\b/.test(label),
  );

  return hasCallControl ? 'in-meeting' : 'not-in-meeting';
}
