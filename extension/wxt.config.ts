import { defineConfig } from 'wxt';

export default defineConfig({
  modules: ['@wxt-dev/module-react'],
  manifest: {
    name: 'Meeting Intelligence',
    description: 'Capture Google Meet and microphone audio and stream it to a local backend.',
    version: '0.1.6',
    permissions: ['activeTab', 'tabCapture', 'offscreen', 'storage', 'sidePanel', 'scripting'],
    host_permissions: ['https://meet.google.com/*', 'http://localhost:8000/*'],
    icons: {
      16: 'meeting-intelligence-icon-16.png',
      32: 'meeting-intelligence-icon-32.png',
      48: 'meeting-intelligence-icon-48.png',
      128: 'meeting-intelligence-icon-128.png',
    },
    action: {
      default_title: 'Open Meeting Intelligence controls',
      default_icon: {
        16: 'meeting-intelligence-icon-16.png',
        32: 'meeting-intelligence-icon-32.png',
        48: 'meeting-intelligence-icon-48.png',
        128: 'meeting-intelligence-icon-128.png',
      },
    },
    side_panel: { default_path: 'sidepanel.html' },
  },
});
