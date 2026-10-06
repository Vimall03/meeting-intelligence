import { defineConfig } from 'wxt';

export default defineConfig({
  modules: ['@wxt-dev/module-react'],
  // WXT loads mode-specific environment files before resolving this callback.
  // Resolve origins here so permissions match the URLs bundled into the app.
  manifest: () => {
    const backendOrigin = new URL(process.env.WXT_BACKEND_URL || 'http://localhost:8000').origin;
    const authOrigin = process.env.WXT_AUTH_API_URL ? new URL(process.env.WXT_AUTH_API_URL).origin : undefined;
    return {
      name: 'Meeting Intelligence',
      description: 'Find related files and turn online meeting follow-ups into Jira tasks. Live capture currently supports Google Meet.',
      version: '0.1.8',
      permissions: ['activeTab', 'tabCapture', 'offscreen', 'storage', 'sidePanel', 'scripting'],
      host_permissions: ['https://meet.google.com/*', `${backendOrigin}/*`, ...(authOrigin ? [`${authOrigin}/*`] : [])],
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
    };
  },
});
