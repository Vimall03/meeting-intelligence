import { microphoneProblem } from '../../lib/user-messages';
import './style.css';

const status = document.querySelector<HTMLElement>('#status')!;
const help = document.querySelector<HTMLElement>('#help')!;
const closeButton = document.querySelector<HTMLButtonElement>('.close')!;

closeButton.addEventListener('click', () => {
  void chrome.runtime.sendMessage({ type: 'CLOSE_MIC_PERMISSION_TAB' });
});

async function requestMicrophonePermission() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    stream.getTracks().forEach((track) => track.stop());
    status.textContent = 'Microphone access granted.';
    help.textContent = 'Returning to Meeting Intelligence…';
    await chrome.runtime.sendMessage({ type: 'MIC_PERMISSION_RESULT', granted: true });
  } catch (error) {
    const errorName = error instanceof Error ? error.name : 'UnknownError';
    status.textContent = microphoneProblem(errorName);
    help.textContent = 'Return to Meeting Intelligence to try again. In Brave’s permission request, choose “forever” under Remember my decision to keep microphone access enabled.';
    await chrome.runtime.sendMessage({ type: 'MIC_PERMISSION_RESULT', granted: false, errorName, errorMessage: microphoneProblem(errorName) }).catch(() => undefined);
  }
}

void requestMicrophonePermission();
