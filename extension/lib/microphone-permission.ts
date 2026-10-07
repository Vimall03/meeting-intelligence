export const MICROPHONE_PERMISSION_KEY = 'microphonePermissionGranted';

// A successful microphone request is remembered across panel visits. Brave
// can report "prompt" from a side panel even after an extension page granted
// access, so only an explicit denial invalidates that successful request.
// getUserMedia still enforces the browser's permission before capture starts.
export function resolveMicrophonePermission(state: PermissionState | undefined, remembered: boolean): 'granted' | 'denied' | 'prompt' {
  if (state === 'denied' || state === 'granted') return state;
  return remembered ? 'granted' : 'prompt';
}

export function isMicrophonePermissionError(name?: string): boolean {
  return name === 'NotAllowedError' || name === 'SecurityError';
}
