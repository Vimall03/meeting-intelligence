import { problem, type MessageCode } from './user-messages.ts';

export type MomMode = 'live_only' | 'mom_only' | 'both';
export interface MomSettings { mode: MomMode; timezone: string; minutes_available: boolean; grace_seconds: number; recovery_retention_seconds: number }
export interface MomMeeting { sessionId: string; roomId: string; captureId: string; mode: MomMode; state: string; finalizeAfter?: number; supportCode?: string }
export const defaultMomSettings: MomSettings = { mode: 'live_only', timezone: 'UTC', minutes_available: false, grace_seconds: 600, recovery_retention_seconds: 86400 };
export const isMomMode = (value: unknown): value is MomMode => ['live_only', 'mom_only', 'both'].includes(value as string);
export function momProblem(data: unknown): string {
  const code = (data as { detail?: { support_code?: string }; support_code?: string })?.detail?.support_code
    ?? (data as { support_code?: string })?.support_code;
  return problem(/^MI-MOM-00[1-9]$|^MI-MOM-01[01]$/.test(code ?? '') ? code as MessageCode : 'MI-MOM-001');
}
export function parseMomSettings(data: unknown): MomSettings {
  const value = data as MomSettings;
  if (!value || !isMomMode(value.mode) || typeof value.minutes_available !== 'boolean'
    || typeof value.timezone !== 'string' || !Number.isFinite(value.grace_seconds)) throw new Error(problem('MI-MOM-001'));
  return value;
}
export const terminalMomStates = ['sent', 'expired', 'empty'];
export function updateMomMeeting(meetings: MomMeeting[], next: MomMeeting): MomMeeting[] {
  // Keep a bounded list of metadata, never audio or transcript content.
  return [next, ...meetings.filter(item => item.sessionId !== next.sessionId)].slice(0, 8);
}
