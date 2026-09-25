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
    status.textContent = `Brave did not grant microphone access (${errorName}).`;
    help.textContent = errorName === 'NotAllowedError' || errorName === 'SecurityError'
      ? 'If you dismissed or previously blocked the prompt, open brave://extensions → Meeting Intelligence → Details → Site settings → Microphone and set it to Allow. Then retry from the side panel.'
      : errorName === 'NotFoundError'
        ? 'No microphone was found. Connect a microphone and retry from the side panel.'
        : error instanceof Error ? error.message : 'Could not access the microphone. Retry from the side panel.';
    await chrome.runtime.sendMessage({ type: 'MIC_PERMISSION_RESULT', granted: false, errorName, errorMessage: error instanceof Error ? error.message : undefined }).catch(() => undefined);
  }
}

void requestMicrophonePermission();
