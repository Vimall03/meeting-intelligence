// Keep this in one place so the local endpoint can be replaced later.
export const MEETING_SOCKET_URL = 'ws://localhost:8000/ws/meeting';

// MediaRecorder emits WebM/Opus chunks every two seconds. The backend should
// accept the first chunk as the container/header context and decode audio/webm.
export const AUDIO_MIME_TYPE = 'audio/webm;codecs=opus';
export const AUDIO_CHUNK_MS = 2_000;
