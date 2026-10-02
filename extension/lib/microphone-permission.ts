export const MICROPHONE_PERMISSION_KEY = 'microphonePermissionGranted';

// A side panel can report "prompt" even after the extension's foreground
// permission page successfully opened the mic. getUserMedia remains the
// authority at capture time; an explicit denial always clears this hint.
export function resolveMicrophonePermission(state: PermissionState | undefined, remembered: boolean): 'granted' | 'denied' | 'prompt' {
  if (state === 'denied') return 'denied';
  return state === 'granted' || remembered ? 'granted' : 'prompt';
}

export function isMicrophonePermissionError(name?: string): boolean {
  return name === 'NotAllowedError' || name === 'SecurityError';
}
