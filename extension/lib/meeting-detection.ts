import type { MeetingStatus } from './types';
import { meetingIdFromUrl, meetingPlatformFromUrl } from './meeting-session.ts';

export function detectMeeting(url = window.location.href, page: Pick<Document, 'querySelectorAll'> = document): MeetingStatus {
  if (!meetingIdFromUrl(url)) {
    return 'not-in-meeting';
  }

  const labels = Array.from(page.querySelectorAll<HTMLElement>(
    'button, [role="button"], [aria-label], [data-tooltip], [title]',
  )).filter((element) => !element.closest('[hidden], [aria-hidden="true"]') && element.getClientRects().length > 0).flatMap((element) => [
    element.getAttribute('aria-label'),
    element.getAttribute('data-tooltip'),
    element.getAttribute('title'),
    element.innerText,
  ]).map((label) => label?.replace(/\s+/g, ' ').trim().toLowerCase() ?? '').filter(Boolean);

  if (meetingPlatformFromUrl(url) === 'zoom') {
    // Waiting rooms and device previews can have a microphone and Leave button.
    // Require the actual meeting toolbar, not just a meeting link or device UI.
    const leave = labels.some((label) => /^(leave|end)( meeting)?(?:\b|,)/.test(label));
    const audio = labels.some((label) => /^(mute|unmute|join audio|audio settings)(?:\b|,)/.test(label));
    const controls = labels.some((label) => /^(participants|share screen|share|reactions|security|host tools|more)(?:\b|,)/.test(label));
    return leave && audio && controls ? 'in-meeting' : 'not-in-meeting';
  }

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
