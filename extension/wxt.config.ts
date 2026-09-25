import { defineConfig } from 'wxt';

export default defineConfig({
  modules: ['@wxt-dev/module-react'],
  manifest: {
    name: 'Meeting Intelligence',
    description: 'Capture Google Meet and microphone audio and stream it to a local backend.',
    version: '0.1.0',
    permissions: ['activeTab', 'tabCapture', 'offscreen', 'storage', 'sidePanel'],
    host_permissions: ['https://meet.google.com/*', 'http://localhost:8000/*'],
    action: {
      default_title: 'Open Meeting Intelligence controls',
    },
    side_panel: { default_path: 'sidepanel.html' },
  },
});
