import { meetingPlatformFromUrl, MEETING_MATCHES } from './meeting-session.ts';

export type ContentScriptManifest = {
  content_scripts?: Array<{
    matches?: string[];
    js?: string[];
  }>;
};

export function meetingContentScriptFiles(manifest: ContentScriptManifest, tabUrl: string | undefined): string[] {
  const platform = meetingPlatformFromUrl(tabUrl);
  if (!platform) return [];
  const match = MEETING_MATCHES[platform === 'google_meet' ? 0 : 1];
  return manifest.content_scripts
    ?.find((script) => script.matches?.includes(match))
    ?.js?.filter(Boolean) ?? [];
}

export async function probeWithContentScriptRecovery<T>({
  probe,
  inject,
  manifest,
  tabUrl,
}: {
  probe: () => Promise<T>;
  inject: (files: string[]) => Promise<void>;
  manifest: ContentScriptManifest;
  tabUrl: string | undefined;
}): Promise<T> {
  try {
    return await probe();
  } catch (originalError) {
    const files = meetingContentScriptFiles(manifest, tabUrl);
    if (files.length === 0) throw originalError;
    await inject(files);
    return probe();
  }
}
