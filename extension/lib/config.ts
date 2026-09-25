// Keep this in one place so the local endpoint can be replaced later.
export const MEETING_SOCKET_URL = 'ws://localhost:8000/ws/meeting';

// Each window is a self-contained WebM/Opus recording sent to the backend.
export const AUDIO_MIME_TYPE = 'audio/webm;codecs=opus';
export const AUDIO_WINDOW_MS = 8_000;
