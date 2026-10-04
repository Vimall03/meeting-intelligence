export const MICROPHONE_PERMISSION_KEY = 'microphonePermissionGranted';

// Prefer the browser's current permission state. The remembered value is only
// a fallback for Brave versions that do not expose microphone permissions to
// extension side panels at all.
export function resolveMicrophonePermission(state: PermissionState | undefined, remembered: boolean): 'granted' | 'denied' | 'prompt' {
  if (state !== undefined) return state;
  return remembered ? 'granted' : 'prompt';
}

export function isMicrophonePermissionError(name?: string): boolean {
  return name === 'NotAllowedError' || name === 'SecurityError';
}
