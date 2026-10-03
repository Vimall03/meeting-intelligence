export type ContentScriptManifest = {
  content_scripts?: Array<{
    matches?: string[];
    js?: string[];
  }>;
};

export function meetContentScriptFiles(manifest: ContentScriptManifest): string[] {
  return manifest.content_scripts
    ?.find((script) => script.matches?.some((match) => match.includes('meet.google.com')))
    ?.js?.filter(Boolean) ?? [];
}

export async function probeWithContentScriptRecovery<T>({
  probe,
  inject,
  manifest,
}: {
  probe: () => Promise<T>;
  inject: (files: string[]) => Promise<void>;
  manifest: ContentScriptManifest;
}): Promise<T> {
  try {
    return await probe();
  } catch (originalError) {
    const files = meetContentScriptFiles(manifest);
    if (files.length === 0) throw originalError;
    await inject(files);
    return probe();
  }
}
