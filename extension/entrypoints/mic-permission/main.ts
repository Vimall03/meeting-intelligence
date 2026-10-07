import { microphoneProblem } from '../../lib/user-messages';
import './style.css';

const status = document.querySelector<HTMLElement>('#status')!;
const help = document.querySelector<HTMLElement>('#help')!;
const closeButton = document.querySelector<HTMLButtonElement>('.close')!;
const requestButton = document.querySelector<HTMLButtonElement>('#request-microphone')!;
let requestPending = false;

closeButton.addEventListener('click', () => {
  void chrome.runtime.sendMessage({ type: 'CLOSE_MIC_PERMISSION_TAB' });
});

async function requestMicrophonePermission() {
  if (requestPending) return;
  requestPending = true;
  requestButton.disabled = true;
  requestButton.textContent = 'Waiting for permission…';
  status.textContent = 'Allow access in your browser’s microphone request.';
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    stream.getTracks().forEach((track) => track.stop());
    status.textContent = 'Microphone access granted.';
    help.textContent = 'Returning to Meeting Intelligence…';
    // Closing the permission tab after a successful response can end its
    // runtime connection. It must not turn a real grant into a false denial.
    await chrome.runtime.sendMessage({ type: 'MIC_PERMISSION_RESULT', granted: true }).catch(() => undefined);
  } catch (error) {
    const errorName = error instanceof Error ? error.name : 'UnknownError';
    const permissionState = await navigator.permissions?.query({ name: 'microphone' as PermissionName })
      .then((permission) => permission.state).catch(() => undefined);
    status.textContent = microphoneProblem(errorName);
    help.textContent = 'Check microphone access in your browser and system settings, then try again here. In Brave, choose “forever” under Remember my decision before allowing access.';
    await chrome.runtime.sendMessage({ type: 'MIC_PERMISSION_RESULT', granted: false, permissionState, errorName, errorMessage: microphoneProblem(errorName) }).catch(() => undefined);
    requestPending = false;
    requestButton.disabled = false;
    requestButton.textContent = 'Try again';
  }
}

requestButton.addEventListener('click', () => {
  void requestMicrophonePermission();
});
