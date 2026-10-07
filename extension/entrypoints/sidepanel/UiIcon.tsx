type IconName = 'settings' | 'close' | 'back' | 'refresh';

export function UiIcon({ name }: { name: IconName }) {
  const paths: Record<IconName, ReactNode> = {
    settings: <><path d="m9.4 3 .6-1h4l.6 1 1.4 2.4 2.8.1L21 9l-1.4 2.4L21 14l-2.2 3.5-2.8.1-1.4 2.4-.6 1h-4l-.6-1L8 17.6l-2.8-.1L3 14l1.4-2.6L3 9l2.2-3.5L8 5.4Z"/><circle cx="12" cy="11.5" r="3"/></>,
    close: <path d="m6 6 12 12M18 6 6 18"/>,
    back: <path d="m14 5-7 7 7 7M7 12h13"/>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 7a7 7 0 0 1 11.5-2L20 8M4 16l2.4 3A7 7 0 0 0 18 17"/></>,
  };
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
import type { ReactNode } from 'react';
